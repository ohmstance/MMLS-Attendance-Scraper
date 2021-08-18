from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from typing import AsyncGenerator, Union
import aiohttp
import asyncio
import copy
import functools
import inspect
import io
import json as _json
import lxml.html
import logging

# ALIASES

AttendanceCache = dict[int, dict[str, Union[int, str]]]

# CONSTANTS

MAX_CONCURRENT_REQUESTS = 8
NETWORK_TIMEOUT = 15
NETWORK_ATTEMPTS = 3
NETWORK_RETRY_BACKOFF = 3
REQUEST_QUEUE_SIZE = 500
MAX_CONTIGUOUS_INVALID_TIMETABLE_ID = 50
MAX_CONTIGUOUS_UNSORTED_TIMETABLE_ID = 50
MAX_TIMETABLE_ID = 500_000
MIN_TIMETABLE_ID = 1


# EXCEPTIONS

class MMLSResponseError(Exception):
    """
    Received an unexpected HTTP response code from MMLS.
    """

    def __init__(self, message: str, status: int):
        self.message = message
        self.status = status

    def __str__(self):
        return self.message


# DECORATORS

def default_connector(func):
    """
    Implements a decorator that checks if a connector isn't passed for a decorated coroutine or async generator. When
    connector is None, it assigns a connector, and closes it after the coroutine or async generator ends.
    """

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        has_internal_connector = False
        if kwargs.get('connector', None) is None:
            connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS)
            kwargs['connector'] = connector
            has_internal_connector = True
        if inspect.isasyncgenfunction(func):
            async def inner():
                try:
                    async for result in func(*args, **kwargs):
                        yield result
                finally:
                    if has_internal_connector:
                        await connector.close()
        else:
            # Assume async function
            async def inner():
                try:
                    return await func(*args, **kwargs)
                finally:
                    if has_internal_connector:
                        await connector.close()
        return inner()

    return wrapped


# COROUTINES

async def _request(method: str, url: str, *,
                   data: Union[aiohttp.FormData, dict] = None,
                   params: Union[dict, tuple, list, str] = None,
                   headers: dict = None,
                   cookies: dict = None,
                   session: aiohttp.ClientSession = None) -> aiohttp.ClientResponse:
    """
    A coroutine that wraps aiohttp.request to enable timeout, retries, and retry backoff time, and max concurrent
    connections.

    method:     typically 'GET' or 'POST'.
    url:        URL to perform HTTP request to.
    data:       typically used in POST request where key-value is encoded as bytes.
    params:     typically used in GET request (e.g. http://xxx.xxx/get?key=value) where key-value is encoded in URL.
    headers:    passes key-value in the request's header.
    cookies:    cookies that should be associated with the request.
    session:    pass shared aiohttp.ClientSession to share connector for connection keepaliving within a session.
    """

    timeout = aiohttp.ClientTimeout(sock_connect=NETWORK_TIMEOUT, sock_read=NETWORK_TIMEOUT)
    if not session:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS)) as session:
            for i in range(NETWORK_ATTEMPTS):
                try:
                    return await session.request(method, url, data=data, params=params, headers=headers,
                                                 cookies=cookies, timeout=timeout)
                except asyncio.TimeoutError:
                    if i == NETWORK_ATTEMPTS - 1:
                        raise
                    await asyncio.sleep(NETWORK_RETRY_BACKOFF)
    for i in range(NETWORK_ATTEMPTS):
        try:
            return await session.request(method, url, data=data, params=params, headers=headers,
                                         cookies=cookies, timeout=timeout)
        except asyncio.TimeoutError:
            if i == NETWORK_ATTEMPTS - 1:
                raise
            await asyncio.sleep(NETWORK_RETRY_BACKOFF)


@default_connector
async def update_cache(attendance_cache: AttendanceCache = None,
                       update_cached: bool = True, start_timetable_id: int = None, end_timetable_id: int = None, *,
                       connector: aiohttp.TCPConnector = None) -> AttendanceCache:
    """
    Accepts attendance cache from Scraper.attendance_cache. It fetches attendance URLs and updates the attendance cache
    with form text field values used for signing attendance. If attendance_cache is not specified, the coroutine starts
    building cache from scratch. It returns a dict of timetable ID to form text field values to be passed into Scraper.

    The coroutine first updates cached attendance URls for classes today and the future if update_cached is True. Then
    it starts fetching future attendance URLs that are not yet cached. Lastly, it fetches attendance URLs in the past
    that aren't cached. In the case when the cache is empty, it starts fetching and populating the cache from the first
    attendance URL with timetable ID of MIN_TIMETABLE_ID. If this is the case, it will take a while before cache is
    up-to-date.

    attendance_cache:   dict object returned by Scraper.attendance_cache.
    update_cached:      if True, updates cached form text field values of attendance URLs today and the future.
    start_timetable_id: timetable ID to start updating cache from.
    end_timetable_id:   timetable ID to stop updating cache.
    connector:          pass aiohttp.TCPConnector to share connector for connection keepaliving.
    """

    attendance_cache = attendance_cache or {}

    async def feeder(t_timetable_ids: list[int], t_session: aiohttp.ClientSession, task_queue: asyncio.Queue) \
            -> None:
        """
        Feeds the queue HTTP request tasks which sends GET requests to attendance URLs with respective timetable ID
        in the timetable ID list.
        """

        for t_timetable_id in t_timetable_ids:
            t_task = asyncio.create_task(
                _request('GET', f"https://mmls.mmu.edu.my/attendance:0:0:{t_timetable_id}", session=t_session)
            )
            try:
                await task_queue.put(t_task)
            except asyncio.CancelledError:
                t_task.cancel()
                raise

    async def update(t_timetable_ids: list[int], t_session: aiohttp.ClientSession) -> None:
        """
        Updates attendance cache entries whose timetable IDs are listed in the timetable ID list.
        """

        task_queue = asyncio.Queue(REQUEST_QUEUE_SIZE)
        feeder_task = asyncio.create_task(feeder(t_timetable_ids, t_session, task_queue))
        logging.info(f"Feeder task with a task queue size of {task_queue.maxsize} was started.")
        try:
            invalid_counter = MAX_CONTIGUOUS_INVALID_TIMETABLE_ID
            previous_timetable_id = -1
            for timetable_id in t_timetable_ids:
                if invalid_counter == 0:
                    logging.info(f"MAX_CONTIGUOUS_INVALID_TIMETABLE_ID was reached. Stopping.")
                    break
                task = await task_queue.get()
                try:
                    response = await task
                    async with response:
                        if response.status == 500:
                            attendance_cache.pop(timetable_id, None)
                            if timetable_id - 1 == previous_timetable_id:
                                invalid_counter -= 1
                            continue
                        elif response.status != 200:
                            raise MMLSResponseError(f'Response status not OK. Received {response.status}',
                                                    response.status)
                        html = lxml.html.fromstring(await response.text())
                    form = {
                        timetable_id: {
                            "starttime": html.xpath("//input[@name='starttime']/@value")[0],
                            "endtime": html.xpath("//input[@name='endtime']/@value")[0],
                            "class_date": html.xpath("//input[@name='class_date']/@value")[0],
                            "class_id": int(html.xpath("//input[@name='class_id']/@value")[0])
                        }
                    }
                    attendance_cache.update(copy.deepcopy(form))
                    invalid_counter = MAX_CONTIGUOUS_INVALID_TIMETABLE_ID
                finally:
                    previous_timetable_id = timetable_id
                    task_queue.task_done()
        finally:
            feeder_task.cancel()
            await asyncio.wait([feeder_task])
            logging.info(f"Feeder task cancelled.")
            while not task_queue.empty():
                task = task_queue.get_nowait()
                task.cancel()
                await asyncio.wait([task])
                task_queue.task_done()
            logging.info(f"Pending tasks cancelled.")

    # Makes sure input timetable ID is within range of MIN_TIMETABLE_ID and MAX_TIMETABLE_ID
    # If no value is passed in argument, defaults to MIN_TIMETABLE_ID and MAX_TIMETABLE_ID
    start_timetable_id = (
        MIN_TIMETABLE_ID if start_timetable_id is None
        else start_timetable_id if start_timetable_id > MIN_TIMETABLE_ID
        else MIN_TIMETABLE_ID
    )
    end_timetable_id = (
        MAX_TIMETABLE_ID if end_timetable_id is None
        else end_timetable_id if end_timetable_id < MAX_TIMETABLE_ID
        else MAX_TIMETABLE_ID
    )
    logging.info(f"Start/end timetable ID: {start_timetable_id}/{end_timetable_id}")

    async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
        
        # Check if the first three cached attendance URLs' date matches online. If not, cache is deemed invalid and cleared.
        attendance_cache_generator = (timetable_id for timetable_id in attendance_cache.keys())
        head_timetable_ids = []
        for _ in range(3):
            try:
                head_timetable_ids.append(next(attendance_cache_generator))
            except StopIteration:
                pass
        tasks = [asyncio.create_task(_request('GET', f"https://mmls.mmu.edu.my/attendance:0:0:{timetable_id}", session=session)) for timetable_id in head_timetable_ids]
        try:
            responses = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
                raise
            await asyncio.wait(tasks)
        for index, response in enumerate(responses):
            async with response:
                if response.status == 500:
                    if index+1 == len(head_timetable_ids):
                        attendance_cache = {}
                        logging.warning(f"Attendance cache mismatch at timetable IDs: {head_timetable_ids}.")
                elif response.status != 200:
                    raise MMLSResponseError(f'Response status not OK. Received {response.status}',
                                            response.status)
                else:
                    html = lxml.html.fromstring(await response.text())
                    requested_class_date = html.xpath("//input[@name='class_date']/@value")[0]
                    cached_class_date = attendance_cache[head_timetable_ids[index]]['class_date']
                    if cached_class_date == requested_class_date:
                        break   
                    elif index+1 == len(head_timetable_ids):
                        attendance_cache = {}
                        logging.warning(f"Attendance cache mismatch at timetable IDs: {head_timetable_ids}.")

        # First update current and future timetable IDs that are cached.
        if update_cached:
            timezone_mst = timezone(timedelta(hours=8))
            timetable_id_range = {
                timetable_id for timetable_id in range(start_timetable_id, end_timetable_id + 1)
            }
            update_timetable_ids = [
                timetable_id for timetable_id, form in attendance_cache.items()
                if (date.fromisoformat(form["class_date"]) - datetime.now(timezone_mst).date()).days >= 0
                and timetable_id in timetable_id_range
            ]
            logging.info(f"Updating current and future cached attendance info")
            logging.debug(f"update_timetable_ids: {update_timetable_ids}")
            await update(update_timetable_ids, session)

        # Next update future timetable IDs (highest timetable ID cached) that are not in cache.
        max_cached_timetable_id = max(attendance_cache, default=None)
        # Skip if cache is empty or highest timetable ID cached goes beyond set maximum timetable ID.
        if max_cached_timetable_id is None or max_cached_timetable_id > MAX_TIMETABLE_ID:
            future_timetable_ids = []
        else:
            future_timetable_ids = [
                timetable_id for timetable_id in range(max_cached_timetable_id + 1, end_timetable_id + 1)
            ]
        logging.info(f"Fetching future uncached attendance info to cache")
        logging.debug(f"future_timetable_ids: {future_timetable_ids}")
        await update(future_timetable_ids, session)

        # Lastly update the rest of the timetable IDs that are not inside the cache.
        future_timetable_ids_set = {timetable_id for timetable_id in future_timetable_ids}
        pending_timetable_ids = [
            timetable_id for timetable_id in range(start_timetable_id, end_timetable_id + 1)
            if timetable_id not in attendance_cache
            and timetable_id not in future_timetable_ids_set
        ]
        logging.info(f"Fetching uncached attendance info to cache")
        logging.debug(f"pending_timetable_ids: {pending_timetable_ids}")
        await update(pending_timetable_ids, session)

        logging.info(f"Update cache done.")
        return attendance_cache


# CLASSES

class Scraper:
    """
    Class that does attendance scraping.

    An attendance cache is maintained to speed up scraping process. The cache could be reassigned, obtained or cleared
    by interacting with the class's Scraper.attendance_cache property.

    attendance_cache:   a dict of attendance cache to be loaded in Scraper.
    allow_caching:      if True, allows caching form input text fields obtained from attendance URLs.
    """

    def __init__(self, attendance_cache: Union[None, str, io.TextIOBase, AttendanceCache] = None, *,
                 allow_caching: bool = True):
        self._attendance_cache: AttendanceCache
        self.attendance_cache = attendance_cache
        self.allow_caching = allow_caching

    def __repr__(self):
        return (self.__class__.__qualname__ +
                f"(attendance_cache={self.attendance_cache}, allow_caching={self.allow_caching})")

    @property
    def attendance_cache(self) -> dict[int, dict[str, Union[int, str]]]:
        """
        Returns the attendance cache.
        """

        return self._attendance_cache

    @attendance_cache.setter
    def attendance_cache(self, ext_cache: Union[None, str, io.TextIOBase, AttendanceCache]):
        """
        Assigns attendance cache to the instance. It accepts json file, json string, or dict of attendance cache, and
        converts any form of passed attendance cache into an internally used format.
        """

        if ext_cache is None:
            attendance_cache = {}
        elif isinstance(ext_cache, io.TextIOBase):
            attendance_cache = _json.load(ext_cache)
        elif isinstance(ext_cache, str):
            attendance_cache = _json.loads(ext_cache)
        elif isinstance(ext_cache, dict):
            attendance_cache = ext_cache
        else:
            raise TypeError(f"'str', 'dict', or text file is expected, not {type(ext_cache)} object.")
        self._attendance_cache: AttendanceCache = {
            int(timetable_id): {
                "starttime": attendance_info["starttime"],
                "endtime": attendance_info["endtime"],
                "class_date": attendance_info["class_date"],
                "class_id": int(attendance_info["class_id"])
            } for timetable_id, attendance_info in attendance_cache.items()
        }

    @attendance_cache.deleter
    def attendance_cache(self):
        """
        Clears attendance cache.
        """

        self._attendance_cache: AttendanceCache = {}

    def attendance_cache_json(self) -> str:
        """
        Returns a json-formatted 'str' of the attendance cache.
        """

        return _json.dumps(self._attendance_cache)

    async def _request_attendance_text_fields(self, timetable_id: int, session: aiohttp.ClientSession) \
            -> Union[AttendanceForm, None]:
        """
        Sends a 'GET' request to a minimal MMLS attendance URL with passed timetable ID and parses the HTML response
        for input text fields in the attendance submission form for start time, end time, class date and class ID. It
        then returns them within an AttendanceForm object.

        This coroutine checks if the requested URL is cached, and if it is, returns the form input text fields from the
        cache instead. If it is not cached, it loads the URL, and saves the input text fields to cache before returning
        it.

        timetable_id:   attendance URL with the timetable ID to be requested.
        session:        pass shared aiohttp.ClientSession to share connector for connection keepaliving within a
                        session.
        """

        attendance_info = self.attendance_cache.get(timetable_id, None)
        if attendance_info is None:
            response = await _request('GET', f"https://mmls.mmu.edu.my/attendance:0:0:{timetable_id}", session=session)
            async with response:
                if response.status == 500:
                    return None
                if response.status != 200:
                    raise MMLSResponseError(f'Response status not OK. Received {response.status}', response.status)
                html = lxml.html.fromstring(await response.text())
            attendance_info = copy.deepcopy({
                "starttime": html.xpath("//input[@name='starttime']/@value")[0],
                "endtime": html.xpath("//input[@name='endtime']/@value")[0],
                "class_date": html.xpath("//input[@name='class_date']/@value")[0],
                "class_id": int(html.xpath("//input[@name='class_id']/@value")[0])
            })
            if self.allow_caching:
                self._attendance_cache.update({timetable_id: attendance_info})
        return AttendanceForm(
            timetable_id=timetable_id,
            start_time=attendance_info["starttime"],
            end_time=attendance_info["endtime"],
            class_date=attendance_info["class_date"],
            class_id=attendance_info["class_id"]
        )

    @default_connector
    async def scrape(self, courses: Courses, start_timetable_id: int, end_timetable_id: int, *,
                     connector: aiohttp.TCPConnector = None) -> AsyncGenerator[DetailedAttendanceForm, None]:
        """
        Scrapes for attendance URLs in the range of start_timetable_id to end_timetable_id and yields attendances
        belonging to classes which are selected in courses. An async generator.

        courses:            a Courses object used to store information about subjects and classes.
        start_timetable_id: the starting timetable ID to start scraping from.
        end_timetable_id:   the end timetable ID to stop scraping at.
        connector:          pass aiohttp.TCPConnector to share connector for connection keepaliving.
        """

        # Maps class ID to Class object associated with classes selected to be scraped for.
        class_id_to_class = {a_class.id: copy.deepcopy(a_class) for a_class in courses.selected_classes}
        logging.debug(f"class_id_to_class={class_id_to_class}")
        pending_timetable_id = [timetable_id for timetable_id in range(start_timetable_id, end_timetable_id + 1)]
        logging.debug(f"pending_timetable_id={pending_timetable_id}")
        async for attendance_form in self._scrape(pending_timetable_id, connector=connector):
            if int(attendance_form.class_id) in class_id_to_class:
                a_class = class_id_to_class[attendance_form.class_id]
                detailed_attendance = DetailedAttendanceForm(
                    timetable_id=int(attendance_form.timetable_id),
                    start_time=attendance_form.start_time,
                    end_time=attendance_form.end_time,
                    class_date=attendance_form.class_date,
                    class_id=int(attendance_form.class_id),
                    class_code=a_class.code,
                    subject_code=a_class.subject.code,
                    subject_name=a_class.subject.name,
                    subject_id=int(a_class.subject.id),
                    coordinator_id=int(a_class.subject.coordinator_id)
                )
                yield detailed_attendance

    @default_connector
    async def scrape_date(self, courses: Courses, start_date: date, end_date: date, *,
                          fast: bool = True,
                          connector: aiohttp.TCPConnector = None,
                          do_requests: bool = True) -> AsyncGenerator[DetailedAttendanceForm, None]:
        """
        Scrapes for attendance URLs within the range of start_date to end_date and yields attendances belonging to
        classes which are selected in courses.

        The async generator first searches the range of timetable IDs which falls within the specified date range. If
        fast is True, it uses binary search (Scraper._dates_to_timetable_id) to find the first and the last instance
        of timetable ID that falls within the date range. If reliability is needed, fast can be set to False, which
        uses binary search (Scraper._get_near_date) to find any timetable ID that is near (1-2 months) to the first
        instance of timetable ID that falls within the specified date range. It starts scraping from the obtained
        timetable ID. Whether fast is set to True or False, the async generator determines whether proceeding attendance
        URLs will be within the specified date range or not. If not, it will abort scraping further.

        courses:            a Courses object used to store information about subjects and classes.
        start_date:         the target date to start scraping from.
        end_date:           the target date to stop scraping beyond.
        fast:               if True, uses binary search to find exact first and last instance of timetable IDs which
                            falls within the date range and scrapes within that timetable ID range. If False, it binary
                            searches for a timetable ID with a date roughly 1-2 months prior to the specified start date
                            and starts scraping from there.
        connector:          pass aiohttp.TCPConnector to share connector for connection keepaliving.
        do_requests:        if False, don't do HTTP requests. Uses cache only.
        """

        # Keeps track of which attendance URLs of selected class are cached.
        matched_cached_timetable_id = set()
        # Maps class ID to Class object associated with classes selected to be scraped for.
        class_id_to_class = {a_class.id: copy.deepcopy(a_class) for a_class in courses.selected_classes}
        logging.debug(f"class_id_to_class={class_id_to_class}")
        # Goes through all the cached attendance page's form input text fields.
        for timetable_id, attendance_info in self._attendance_cache.items():
            # Matches only within the desired date range.
            if start_date <= date.fromisoformat(attendance_info["class_date"]) <= end_date:
                # Adds current timetable ID to a set of cached timetable IDs that matched.
                matched_cached_timetable_id.add(timetable_id)
                # Matches only attendance URLs belonging to selected classes.
                if attendance_info["class_id"] in class_id_to_class:
                    detailed_attendance = DetailedAttendanceForm(
                        timetable_id=timetable_id,
                        start_time=attendance_info["starttime"],
                        end_time=attendance_info["endtime"],
                        class_date=attendance_info["class_date"],
                        class_id=attendance_info["class_id"],
                        class_code=class_id_to_class[attendance_info["class_id"]].code,
                        subject_code=class_id_to_class[attendance_info["class_id"]].subject.code,
                        subject_name=class_id_to_class[attendance_info["class_id"]].subject.name,
                        subject_id=class_id_to_class[attendance_info["class_id"]].subject.id,
                        coordinator_id=class_id_to_class[attendance_info["class_id"]].subject.coordinator_id
                    )
                    yield detailed_attendance

        # Immediately stop after finishing search from cache.
        if not do_requests:
            return

        # Use fast approach by looking for the exact timetable ID range to scrape by binary search so as to not spend
        # too much time fetching attendance URLs that don't belong within the specified date range.
        if fast:
            # Binary search the exact first and last timetable ID of specified date range. As it does binary search, and
            # sometimes some attendance URLs are unsorted, it is occasionally unreliable.
            start_timetable_id, end_timetable_id = await self._dates_to_timetable_ids(start_date, end_date,
                                                                                      connector=connector)
            # The timetable ID associated with the date range may or may not exist, or date-unsorted attendance URLs got
            # in the way of binary search.
            if start_timetable_id is None or end_timetable_id is None:
                logging.warning(f"Timetable ID range within desired date range {start_date.isoformat()} - "
                                f"{end_date.isoformat()} does not exist. Abort.")
                return
        # Use a more reliable approach by looking for a timetable ID to start the scrape with, which has a date near
        # enough to the beginning of the specified date range, thus reducing the amount of fetched attendance URLs not
        # within the specified date range.
        else:
            # Returns a timetable ID which has an associated date that is within a few months prior to start_date. This
            # reduces the amount of redundant attendance URLs being searched. Searching any timetable ID with an
            # associated date that is in the range of months, considering that the timetable ID range is majorly sorted
            # by date has a good enough reliability not to cause some timetable ID that belongs to the specified date
            # range from being missed.
            start_timetable_id = await self._get_near_date(start_date, connector=connector)
            # Set the upper ceiling of timetable ID number to be searched. With the assumption that timetable IDs are
            # largely sorted by date, this generator will stop scraping once it determines proceeding timetable IDs
            # won't belong to the specified date range.
            end_timetable_id = MAX_TIMETABLE_ID
            # Searched too far into the future.
            if start_timetable_id is None:
                logging.warning(f"Timetable ID near {start_date.isoformat()} isn't found. Input date may be too far"
                                f"into the future. Abort.")
                return

        # If a scrape-by-date for attendance URLs within the cache was done, we won't need to search any attendance URLs
        # that are cached.
        pending_timetable_id = [timetable_id for timetable_id in range(start_timetable_id, end_timetable_id + 1)
                                if timetable_id not in matched_cached_timetable_id]
        logging.debug(f"pending_timetable_id={pending_timetable_id}")

        # If timetable ID exceeds this value, scraping has entered date range for timetable IDs. If cache is empty,
        # this variable isn't used. An unattainable timetable ID is assigned as placeholder so comparison to it never
        # yields True.
        min_cached_timetable_id = min(matched_cached_timetable_id, default=MAX_TIMETABLE_ID + 1)
        logging.debug(f"min_cached_timetable_id={min_cached_timetable_id}")
        entered_date_range = False
        dates_outside_range_count = MAX_CONTIGUOUS_UNSORTED_TIMETABLE_ID
        async for attendance_form in self._scrape(pending_timetable_id, connector=connector):
            class_date = date.fromisoformat(attendance_form.class_date)
            # Assumes dates are in increasing order and contiguous
            if not entered_date_range and (start_date <= class_date <= end_date
                                           or attendance_form.timetable_id >= min_cached_timetable_id):
                entered_date_range = True
                logging.info(f"Date range entered at timetable ID: {attendance_form.timetable_id}.")
            # Accounts for sequences of date-unsorted attendance URLS
            if entered_date_range:
                if not start_date <= class_date <= end_date:
                    dates_outside_range_count -= 1
                else:
                    dates_outside_range_count = MAX_CONTIGUOUS_UNSORTED_TIMETABLE_ID
                # Confidently assumes all proceeding attendance URLs won't be within date range.
                if dates_outside_range_count == 0:
                    logging.info(f"MAX_CONTIGUOUS_UNSORTED_TIMETABLE_ID was reached. Stopping.")
                    break
            if start_date <= class_date <= end_date:
                if int(attendance_form.class_id) in class_id_to_class:
                    a_class = class_id_to_class[attendance_form.class_id]
                    detailed_attendance = DetailedAttendanceForm(
                        timetable_id=attendance_form.timetable_id,
                        start_time=attendance_form.start_time,
                        end_time=attendance_form.end_time,
                        class_date=attendance_form.class_date,
                        class_id=attendance_form.class_id,
                        class_code=a_class.code,
                        subject_code=a_class.subject.code,
                        subject_name=a_class.subject.name,
                        subject_id=a_class.subject.id,
                        coordinator_id=a_class.subject.coordinator_id
                    )
                    yield detailed_attendance

    @default_connector
    async def _scrape(self, timetable_ids: list[int], *,
                      connector: aiohttp.TCPConnector = None) -> AsyncGenerator[AttendanceForm, None]:
        """
        Accepts a list of timetable IDs. It then sends requests to attendance URLs with the timetable IDs and yields
        information about the attendance URLs in AttendanceForm.

        This async generator mitigates against HTTP status 500 holes existing in the attendance URLs timetable ID range.
        It also stops fetching once it determines proceeding attendance URLs with timetable IDs that are listed are not
        generated yet.

        timetable_ids:      a list of timetable IDs whose associated attendance URLs are to be fetched.
        connector:          pass aiohttp.TCPConnector to share connector for connection keepaliving.
        """

        task_queue = asyncio.Queue(REQUEST_QUEUE_SIZE)
        async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
            async def feeder() -> None:
                for t_timetable_id in timetable_ids:
                    t_task = asyncio.create_task(self._request_attendance_text_fields(t_timetable_id, session))
                    try:
                        await task_queue.put(t_task)
                    except asyncio.CancelledError:
                        t_task.cancel()
                        raise

            feeder_task = asyncio.create_task(feeder())
            logging.info(f"Feeder task with a task queue size of {task_queue.maxsize} was started.")
            try:
                invalid_counter = MAX_CONTIGUOUS_INVALID_TIMETABLE_ID
                previous_timetable_id = -1
                for timetable_id in timetable_ids:
                    if invalid_counter == 0:
                        logging.info(f"MAX_CONTIGUOUS_INVALID_TIMETABLE_ID was reached. Stopping.")
                        break
                    task = await task_queue.get()
                    attendance_form = await task
                    if attendance_form is not None:
                        yield attendance_form
                        invalid_counter = MAX_CONTIGUOUS_INVALID_TIMETABLE_ID
                    else:
                        if timetable_id - 1 == previous_timetable_id:
                            invalid_counter -= 1
                    previous_timetable_id = timetable_id
                    task_queue.task_done()
            finally:
                feeder_task.cancel()
                await asyncio.wait([feeder_task])
                logging.info(f"Feeder task cancelled.")
                while not task_queue.empty():
                    task = task_queue.get_nowait()
                    task.cancel()
                    await asyncio.wait([task])
                    task_queue.task_done()
                logging.info(f"Pending tasks cancelled.")

    @default_connector
    async def _dates_to_timetable_ids(self, start_date: date, end_date: date, *,
                                      search_between: bool = True, connector: aiohttp.TCPConnector = None) \
            -> Union[tuple[int, int], None]:
        """
        Returns a tuple, where the first and the second integer element describes the first and the last timetable ID
        within the date range respectively.

        The coroutine obtains the range of timetable IDs by doing a binary search on a range of timetable IDs for either
        the first occurrence or the last occurrence of a given date. This function rapidly becomes unreliable as more
        'invalidated' attendance links are produced which will remain in existence until all existing attendance links
        of the previous trimesters are cleared from MMLS.

        start_date:     the date to look for to find the first instance of timetable ID in date range.
        end_date:       the date to look for to find the last instance of timetable ID in date range.
        search_between: in the case search_between is True, if timetable ID with either start or end date isn't found,
                        it searches for dates proceeding or prior or and end date respectively. If search_between is
                        False, it stops and returns on first unsuccessful try.
        connector:      pass aiohttp.TCPConnector to share connector for connection keepaliving.
        """

        async def date_to_timetable_id(t_date: date, t_session: aiohttp.ClientSession, find_head: bool = True) \
                -> Union[int, None]:
            """
            Searches for the head or the tail end of the range of timetable IDs of the desired date.
            """
            upper_bound, lower_bound = MAX_TIMETABLE_ID, MIN_TIMETABLE_ID
            while True:
                current_timetable_id = (upper_bound + lower_bound) // 2
                form = await self._request_attendance_text_fields(current_timetable_id, t_session)

                # If HTTP response status code 500 is encountered, assume link is not generated yet. Note that this is
                # unreliable as error 500 can be produced from deleted or 'invalidated' attendance links.
                if form is None:
                    upper_bound = current_timetable_id - 1
                    continue
                current_date = date.fromisoformat(form.class_date)

                # If date of fetched timetable ID is earlier than desired date, any timetable IDs less than this is
                # irrelevant as wanted date is after it.
                if (t_date - current_date).days > 0:
                    lower_bound = current_timetable_id + 1

                # If date of fetched timetable ID is later than desired date, any timetable IDs more than this is
                # irrelevant as wanted date is before it.
                elif (t_date - current_date).days < 0:
                    upper_bound = current_timetable_id - 1

                # Branch here if this function is set to search for the head of the timetable ID range with the
                # desired date.
                elif find_head:
                    form = await self._request_attendance_text_fields(current_timetable_id - 1, t_session)

                    # If NoneType is returned it could only mean HTTP error code 500 is received.
                    # This is unexpected in this case.
                    if form is None:
                        raise MMLSResponseError(f"Response status not OK. Received 500.", 500)

                    # Next timetable ID is of a different date. Assuming timetable IDs are sorted by date, this is the
                    # head. Return.
                    if date.fromisoformat(form.class_date) != current_date:
                        return current_timetable_id

                    # Current timetable ID is not the head, check the next one.
                    upper_bound = current_timetable_id - 1

                # Branch here instead if this function is set to search for the tail end of the timetable ID range with
                # the desired date.
                else:
                    form = await self._request_attendance_text_fields(current_timetable_id + 1, t_session)

                    # The next timetable ID produces status code 500, assume this is the tail as the continuing
                    # timetable ID is assumed not generated, yet. Return.
                    if form is None:
                        return current_timetable_id

                    # Next timetable ID is of a different date. Assuming timetable IDs are sorted by date, this is the
                    # tail end. Return.
                    if date.fromisoformat(form.class_date) != current_date:
                        return current_timetable_id

                    # Current timetable ID is not the tail, check the next one.
                    lower_bound = current_timetable_id + 1

                # A class session with the desired date does not exist. Abort.
                if upper_bound < lower_bound:
                    return None

        async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
            start_timetable_id, end_timetable_id = None, None
            while start_timetable_id is None or end_timetable_id is None:

                # First pass: Creates task to search the head or the tail of the range of
                # timetable IDs whose class session falls within the desired date.
                # Consequent passes: None means a class session with the searched date is
                # not found in the previous pass.
                if start_timetable_id is None:
                    start_timetable_id_task = asyncio.create_task(
                        date_to_timetable_id(start_date, session, find_head=True)
                    )
                if end_timetable_id is None:
                    end_timetable_id_task = asyncio.create_task(
                        date_to_timetable_id(end_date, session, find_head=False)
                    )

                # If timetable IDs are not found, assign none to the variables.
                start_timetable_id = start_timetable_id or await start_timetable_id_task
                end_timetable_id = end_timetable_id or await end_timetable_id_task

                # If function is not set to search for timetables IDs in between the dates
                # entered for search, regardless whether a timetable ID is found for the
                # desired date or not, abort on first attempt.
                if not search_between:
                    break

                # Search for timetable IDs with dates in between entered dates in one day
                # steps for the date whose timetable ID is not found.
                if start_timetable_id is None:
                    start_date += timedelta(days=1)
                if end_timetable_id is None:
                    end_date -= timedelta(days=1)

                # Timetable IDs with the desired date parameters does not exist. Abort.
                if start_date > end_date:
                    break

            return start_timetable_id, end_timetable_id

    @default_connector
    async def _get_near_date(self, target_date: date, *, connector: aiohttp.TCPConnector = None) -> Union[int, None]:
        """
        Returns a timetable ID which is 1-2 months prior to target timetable ID as a trade-off for speed in exchange
        of reliability. The objective is to reduce the amount of redundant timetable ID that needs to be checked instead
        of completely eliminating all redundant timetable IDs.

        The coroutine does a binary search for any timetable ID within the date range, and each intermediate timetable
        IDs has their adjacent IDs checked to reduce the likelihood of premature end of date search from encountering
        HTTP status 500 holes arising from invalidated attendance URLs. This makes scrape by date slower --  its binary
        equivalent and faster than linear searching from MIN_TIMETABLE_ID.

        target_date:    the date to find a timetable ID near to.
        connector:      pass aiohttp.TCPConnector to share connector for connection keepaliving.
        """

        def get_timetable_ids(n_timetable_id: int, probe_gap: int = 25, probe_count: int = 5) -> list[int]:
            """
            Returns a list of timetable IDs with the input timetable ID preferably at the middle of the list.
            It conforms to MIN_TIMETABLE_ID and MAX_TIMETABLE_ID limits.
            """
            n_lower_bound, n_upper_bound = MIN_TIMETABLE_ID, MAX_TIMETABLE_ID
            l_timetable_id = n_timetable_id - 2 * probe_gap

            # Produces [x-2*gap, x-gap, x, x+gap, x+2*gap].
            probe_timetable_id = [timetable_id for timetable_id in
                                  range(l_timetable_id, l_timetable_id + probe_count * probe_gap + 1, probe_gap)]

            # Produces [x, x+gap, x+2*gap, x+3*gap, x+4*gap].
            if probe_timetable_id[0] < n_lower_bound:
                probe_timetable_id = [timetable_id + 2 * probe_gap for timetable_id in probe_timetable_id]

            # Produces [x-4*gap, x-3*gap, x-2*gap, x-gap, x].
            elif probe_timetable_id[4] > n_upper_bound:
                probe_timetable_id = [timetable_id - 2 * probe_gap for timetable_id in probe_timetable_id]

            return probe_timetable_id

        async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
            upper_bound, lower_bound = MAX_TIMETABLE_ID, MIN_TIMETABLE_ID
            while True:
                current_timetable_id = (upper_bound + lower_bound) // 2
                current_timetable_ids = get_timetable_ids(current_timetable_id)

                task_list = [asyncio.create_task(self._request_attendance_text_fields(timetable_id, session))
                             for timetable_id in current_timetable_ids]
                form_list = await asyncio.gather(*task_list)

                # If every timetable ID that is checked are NoneTypes (HTTP code 500), with a degree of confidence, it
                # can be assumed that they are not generated yet.
                if form_list.count(None) == len(form_list):
                    upper_bound = current_timetable_id - 1
                    continue

                try:
                    # Find an AttendanceForm object with the specified timetable ID.
                    form = next(form for form in form_list
                                if form is not None
                                and form.timetable_id == current_timetable_id)
                except StopIteration:
                    # Pick any AttendanceForm that exists. Normally AttendanceForm with timetable ID nearest to current
                    # timetable ID should be picked, but the timetable IDs are near enough that the date won't change,
                    # and in the context of searching a range of months, this won't matter.
                    form = next(form for form in form_list if form is not None)
                current_date = date.fromisoformat(form.class_date)

                # If date of fetched timetable ID is earlier than desired date range, any timetable IDs less than this
                # is irrelevant as wanted date range is after it.
                if (target_date - current_date).days > 30 * 2:
                    lower_bound = current_timetable_id + 1

                # If date of fetched timetable ID is later than desired date range, any timetable IDs more than this is
                # irrelevant as wanted date range is before it.
                elif (target_date - current_date).days < 30 * 1:
                    upper_bound = current_timetable_id - 1

                # The current timetable ID has an attendance link associated that has a date near enough to the desired
                # date. Return this timetable ID.
                else:
                    return current_timetable_id

                # If the final timetable ID is MIN_TIMETABLE_ID, then assume the timetable ID with the associated date
                # that far back doesn't exist.
                if current_timetable_id == MIN_TIMETABLE_ID:
                    return current_timetable_id

                # Either the user searched too far into the future or that the university hasn't been operating in a
                # while.
                if upper_bound < lower_bound:
                    return None


# CONTAINERS

class Courses:
    """
    A container for subjects and classes with built-in methods.

    Attributes: subjects, classes, selected_classes, json
    """

    class Subject:
        """
        A container for description about a subject and its classes.

        Attributes: id, code, name, coordinator_id, classes, selected_classes
        """

        def __init__(self, courses: Courses, subject_id: int, subject_code: str = None, subject_name: str = None,
                     coordinator_id: int = None):
            self.id = subject_id
            self.code = subject_code
            self.name = subject_name
            self.coordinator_id = coordinator_id
            self._courses = courses
            self._classes: list[Courses.Class] = []

        def __repr__(self):
            return (self.__class__.__qualname__ +
                    f"(id={self.id}, code={self.code}, name={self.name}, coordinator_id={self.coordinator_id}, "
                    f"classes={self._classes})")

        @property
        def classes(self) -> list[Courses.Class]:
            """
            Returns a list of Class objects.
            """

            return self._classes

        @classes.deleter
        def classes(self):
            """
            Removes all references to Class objects.
            """

            self._classes = []

        @property
        def selected_classes(self) -> list[Courses.Class]:
            """
            Returns a list of Class objects whose attribute 'selected' is True.
            """

            return [a_class for a_class in self._classes if a_class.selected]

        def add_class(self, class_id: int, class_code: str = None, selected: bool = False) -> Courses.Class:
            """
            Adds a class (Class object) to an internal list. Checks if there is an already a class with the same
            class ID. If so, it replaces that class with the added one. It returns the created Class object.
            """

            new_class = Courses.Class(self, class_id, class_code, selected)
            for idx, a_class in enumerate(self._classes):
                if a_class.id == new_class.id:
                    self._classes[idx] = new_class
                    return new_class
            self._classes.append(new_class)
            return new_class

        def remove(self) -> bool:
            """
            Removes reference of Subject object (self) in Courses object (parent). Returns True if reference
            exists, and False otherwise.
            """

            try:
                self._courses._subjects.remove(self)
                return True
            except ValueError:
                return False

    class Class:
        """
        A container for description about a class.

        Attributes: id, code, selected, subject
        """

        def __init__(self, subject: Courses.Subject, class_id: int, class_code: str = None, selected: bool = False):
            self.id = class_id
            self.code = class_code
            self.selected = selected
            self._subject = subject

        def __repr__(self):
            return self.__class__.__qualname__ + f"(id={self.id}, code={self.code}, selected={self.selected})"

        @property
        def subject(self) -> Courses.Subject:
            """
            Returns parent Subject object.
            """

            return self._subject

        def remove(self) -> bool:
            """
            Removes reference of Class object (self) in Subject object (parent). Returns True if reference exists, and
            False otherwise.
            """

            try:
                self._subject.classes.remove(self)
                return True
            except ValueError:
                return False

    def __init__(self):
        self._subjects: list[Courses.Subject] = []

    def __repr__(self):
        return self.__class__.__qualname__ + f"(subjects={self._subjects})"

    @property
    def subjects(self) -> list[Subject]:
        """
        Returns a list of Subject objects.
        """

        return self._subjects

    @subjects.deleter
    def subjects(self):
        """
        Removes all references to Subject objects.
        """

        self._subjects = []

    @property
    def classes(self) -> list[Class]:
        """
        Returns a list of Class objects contained in each Subject objects.
        """

        return [a_class for subject in self._subjects for a_class in subject.classes]

    @classes.deleter
    def classes(self):
        """
        Removes all references to Class objects in each Subject objects.
        """

        for subject in self._subjects:
            del subject.classes

    @property
    def selected_classes(self) -> list[Class]:
        """
        Returns a list of Class objects in each Subject objects whose attribute 'selected' is True.
        """

        return [a_class for a_class in self.classes if a_class.selected]

    def add_subject(self, subject_id: int, subject_code: str = None, subject_name: str = None,
                    coordinator_id: int = None) -> Subject:
        """
        Adds a subject (Subject object) to an internal list. Checks if there is an already a subject with the same
        subject ID. If so, it replaces that subject with the added one. It returns the created Subject object.
        """

        new_subject = self.Subject(self, subject_id, subject_code, subject_name, coordinator_id)
        for idx, subject in enumerate(self._subjects):
            if subject.id == subject_id:
                self._subjects[idx] = new_subject
                return new_subject
        self._subjects.append(new_subject)
        return new_subject

    def update(self, courses: Courses) -> None:
        """
        Updates subjects from a Courses object. Checks for subjects with the same subject ID. If there is, it replaces
        it with the subject from given Courses object.

        courses:    Courses object.
        """

        foreign_subjects = copy.deepcopy(courses.subjects)
        for idx, subject in enumerate(self._subjects):
            for foreign_idx, foreign_subject in enumerate(foreign_subjects):
                if subject.id == foreign_subject.id:
                    foreign_subject._courses = self
                    self._subjects[idx] = foreign_subject
                    del foreign_subjects[foreign_idx]
        self._subjects.extend(foreign_subjects)

    def get_subject_by_id(self, subject_id: int) -> Subject:
        """
        Returns a Subject object with matching attribute 'id'. NoneType if it does not exist.
        """

        return next((subject for subject in self.subjects if subject.id == subject_id), None)

    def get_class_by_id(self, class_id: int) -> Class:
        """
        Returns a Class object nested in each Subject object with matching attribute 'id'. NoneType if it does not
        exist.
        """

        return next((a_class for a_class in self.classes if a_class.id == class_id), None)

    @default_connector
    async def load_online(self, user_id, password, *, connector=None):
        """
        Loads registered subjects and all classes in those subjects into a Courses object. It needs student ID and
        password to parse subjects and classes. If credentials are incorrect, it returns False, but True if it succeeds.

        user_id:    MMLS student ID.
        password:   MMLS student password.
        connector:  pass aiohttp.TCPConnector to share connector for connection keepaliving.
        """

        async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
            courses_tmp = Courses()

            # OBTAIN LOGIN TOKEN
            response = await _request('GET', 'https://mmls.mmu.edu.my/', session=session)
            async with response:
                if response.status != 200:
                    raise MMLSResponseError(f'Response status not OK. Received {response.status}', response.status)
                html = lxml.html.fromstring(await response.text())
            token = html.xpath("//input[@name='_token']/@value")[0]
            logging.info(f"Obtained MMLS login form token.")
            logging.debug(f"token: {token}")

            # LOGIN TO MMLS
            data = {'stud_id': user_id, 'stud_pswrd': password, '_token': token}
            response = await _request('POST', 'https://mmls.mmu.edu.my/checklogin', data=data, session=session)
            async with response:
                if response.status == 500:
                    # Invalid credentials
                    logging.warning(f"Input MMLS login credentials are invalid. Abort.")
                    return False
                if response.status != 200:
                    raise MMLSResponseError(f'Response status not OK. Received {response.status}', response.status)
                html = lxml.html.fromstring(await response.text())
            logging.info(f"Logged in to MMLS for user ID: {user_id}")

            # PARSE SUBJECTS
            subject_xpath = "//div[@class='list-group ' and @style='margin-top:-15px']/span/a[1]"
            subject_code_subject_name = [name.split(' - ') for name in html.xpath(f"{subject_xpath}/text()")]
            subject_id_coordinator_id = [link[24:].split(':') for link in html.xpath(f"{subject_xpath}/@href")]
            code_name_ids = [[element for nested_list in zipped for element in nested_list]
                             for zipped in zip(subject_code_subject_name, subject_id_coordinator_id)]
            for subject_code, subject_name, subject_id, coordinator_id in code_name_ids:
                courses_tmp.add_subject(int(subject_id), subject_code, subject_name, int(coordinator_id))
            logging.info(f"{len(courses_tmp.subjects)} registered subjects parsed.")
            logging.debug(f"courses_tmp.subjects: {courses_tmp.subjects}")

            # PARSE CLASSES
            async def parse_classes(t_subject):
                t_subject_id, t_coordinator_id = t_subject.id, t_subject.coordinator_id
                t_response = await _request('GET',
                                            f"https://mmls.mmu.edu.my/studentlist:{t_subject_id}:{t_coordinator_id}:0",
                                            session=session)
                async with t_response:
                    if t_response.status != 200:
                        raise MMLSResponseError(f'Response status not OK. Received {t_response.status}',
                                                t_response.status)
                    t_html = lxml.html.fromstring(await t_response.text())
                t_class_ids = t_html.xpath("//select[@id='select_class']/*[not(self::option[@value='0'])]/@value")
                t_class_codes = t_html.xpath("//select[@id='select_class']/*[not(self::option[@value='0'])]/text()")
                for t_class_id, t_class_code in zip(t_class_ids, t_class_codes):
                    t_subject.add_class(int(t_class_id), t_class_code)

            tasks = [asyncio.create_task(parse_classes(subject)) for subject in courses_tmp.subjects]
            try:
                await asyncio.wait(tasks)
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.wait(tasks)
                raise
            logging.info(f"{len(courses_tmp.classes)} classes parsed.")
            logging.debug(f"courses_tmp.classes: {courses_tmp.classes}")

            # UPDATE COURSES
            self.update(courses_tmp)
            logging.info(f"Courses updated.")
            logging.debug(f"{self.__class__.__qualname__}: {self}")

            # LOGOUT
            response = await _request('GET', 'https://mmls.mmu.edu.my/logout', session=session)
            async with response:
                if response.status != 200:
                    raise MMLSResponseError(f'Response status not OK. Received {response.status}', response.status)
            logging.info(f"Logged out from MMLS for user ID: {user_id}.")
            return True

    @default_connector
    async def autoselect_classes(self, user_id, *, connector=None):
        """
        Autoselects classes in a Courses object that the student ID has registered for in the current trimester.

        user_id:    MMLS student ID.
        connector:  pass aiohttp.TCPConnector to share connector for connection keepaliving.
        """

        async def select_if_registered(t_class):
            # Cannot share the same session but can share the same connector
            async with aiohttp.ClientSession(connector=connector, connector_owner=False) as t_session:
                t_data = {'class_id': t_class.id, 'stud_id': user_id, 'stud_pswrd': '0'}
                t_headers = {'Referer': 'https://mmls.mmu.edu.my/attendance:0:0:1'}
                t_response = await _request('POST', 'https://mmls.mmu.edu.my/attendancelogin',
                                            data=t_data, headers=t_headers, session=t_session)
                async with t_response:
                    if t_response.status != 200:
                        raise MMLSResponseError(f'Response status not OK. Received {t_response.status}.',
                                                t_response.status)
                    t_html = lxml.html.fromstring(await t_response.text())
                if not t_html.xpath("//div[@class='alert alert-danger']/text()='You are not register to this class.'"):
                    t_class.selected = True

        tasks = [asyncio.create_task(select_if_registered(a_class)) for a_class in self.classes]
        try:
            await asyncio.wait(tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
                raise
            await asyncio.wait(tasks)
        logging.info(f"Selected {len(self.selected_classes)} classes.")
        logging.debug(f"{self.__class__.__qualname__}.selected_classes: {self.selected_classes}")

    def json(self) -> str:
        """
        Return subjects and classes in json-formatted str representation.
        """

        subjects = [{
            'id': subject.id,
            'code': subject.code,
            'name': subject.name,
            'coordinator_id': subject.coordinator_id,
            'classes': [{
                'id': a_class.id,
                'code': a_class.code,
                'selected': a_class.selected
            } for a_class in subject.classes]
        } for subject in self.subjects]
        return _json.dumps(subjects)

    def load_json(self, json_str: str, *, update: bool = False) -> None:
        """
        Loads subjects and classes from json-formatted str of Courses.

        json_str:   json-formatted string representation of Courses.
        update:     if True, replaces same subject and adds unincluded subjects to current Courses instance. If False,
                    it removes all existing subjects before loading subjects from json string.
        """

        subject_list = _json.loads(json_str)
        courses = Courses()
        for subject in subject_list:
            subject_obj = courses.add_subject(
                subject_id=int(subject['id']),
                subject_code=subject['code'],
                subject_name=subject['name'],
                coordinator_id=int(subject['coordinator_id'])
            )
            for a_class in subject['classes']:
                subject_obj.add_class(
                    class_id=int(a_class['id']),
                    class_code=a_class['code'],
                    selected=a_class['selected']
                )
        if not update:
            self._subjects = []
        self.update(courses)


class AttendanceForm:
    """
    A container used to store form text field values, used during submitting attendance, from an attendance url with
    respective timetable ID.

    timetable_id:   Every class session has an attendance link, which has a unique timetable ID.
    start_time:     The start time of the class session.
    end_time:       The end time of the class session.
    class_date:     The date of the class session it is held on.
    class_id:       Every course/subject has at least a class, and each of them has their own class ID.
    """

    __slots__ = 'timetable_id', 'start_time', 'end_time', 'class_date', 'class_id'

    def __init__(self, timetable_id: int, start_time: str, end_time: str, class_date: str, class_id: int):
        self.timetable_id = timetable_id
        self.start_time = start_time
        self.end_time = end_time
        self.class_date = class_date
        self.class_id = class_id

    def __repr__(self):
        return (self.__class__.__qualname__ +
                f"(timetable_id={self.timetable_id}, start_time={self.start_time}, end_time={self.end_time}, "
                f"class_date={self.class_date}, class_id={self.class_id})")


class DetailedAttendanceForm(AttendanceForm):
    """
    A container used to store, in addition to form text field values bound to the respective timetable ID, class code,
    coordinator ID, subject ID, subject code, and subject name, which would be provided by the login function, or
    manually filled in by the user depending on the implementation by the developer. The information provided are used
    to create attendance URLs and attendance list URLs. It inherits AttendanceForm class.

    class_code:     Every class, in addition to their class ID, has its own non-unique class code (e.g. EC01, ECA2).
    coordinator_id: Every subject/course has a coordinator, and each coordinator has a unique ID. Required to craft
                    attendance list URLs.
    subject_id:     Every subject/course has a unique ID. Required to craft attendance list URLs.
    subject_code:   Every subject/course has a unique subject code, though they can share the same name. (e.g. MPU4116)
    subject_name:   Every subject/course has a name, and it does not have to be unique. (e.g. WORKPLACE COMMUNICATION)
    """

    def __init__(self, timetable_id: int, start_time: str, end_time: str, class_date: str, class_id: int,
                 class_code: str = None, coordinator_id: int = None, subject_id: int = None, subject_code: str = None,
                 subject_name: str = None):
        AttendanceForm.__init__(self, timetable_id, start_time, end_time, class_date, class_id)
        self.class_code = class_code
        self.coordinator_id = coordinator_id
        self.subject_id = subject_id
        self.subject_code = subject_code
        self.subject_name = subject_name

    def __repr__(self):
        return (self.__class__.__qualname__ +
                f"(timetable_id={self.timetable_id}, start_time={self.start_time}, end_time={self.end_time}, "
                f"class_date={self.class_date}, class_id={self.class_id}, class_code={self.class_code}, "
                f"coordinator_id={self.coordinator_id}, subject_id={self.subject_id}, "
                f"subject_code={self.subject_code}, subject_name={self.subject_name})")

    @property
    def attendance_url(self) -> Union[str, None]:
        """
        Creates and returns an attendance URL.
        """

        return (f"https://mmls.mmu.edu.my/attendance:{self.subject_id or '0'}:{self.coordinator_id or '0'}:"
                f"{self.timetable_id}")

    @property
    def attendance_list_url(self) -> Union[str, None]:
        """
        Creates and returns an attendance list URL.
        """

        if self.subject_id and self.coordinator_id:
            return (f"https://mmls.mmu.edu.my/viewAttendance:{self.subject_id}:{self.coordinator_id}:"
                    f"{self.timetable_id}:{self.class_id}:1")
        logging.warning(f"Couldn't produce an attendance list URL because attribute subject_id or coordinator_id is "
                        f"empty. {self.__class__.__qualname__}.subject_id: {self.subject_id}, "
                        f"{self.__class__.__qualname__}.coordinator_id: {self.coordinator_id}")
        return None
