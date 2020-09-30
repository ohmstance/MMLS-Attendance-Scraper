from datetime import date, timedelta
from io import StringIO
from lxml import etree
import aiohttp
import asyncio
import sqlite3
import atexit
import functools
import json as ijson
import copy

MAX_CONCURRENT_REQUESTS = 8
NETWORK_TIMEOUT = 15
NETWORK_RETRIES = 3
NETWORK_RETRY_BACKOFF = 3
MAX_TIMETABLE_ID = 120_000
MIN_TIMETABLE_ID = 1
CONNECT_DB = True # TODO: Implement attendance url caching via SQLite3

class MMLSResponseError(Exception):
    def __init__(self, message, status):
        self.message = message
        self.status = status
    def __str__(self):
        return self.message

global_connector = None #Set max concurrent requests here with aiohttp.TCPConnector(limit=[max_concurrent_requests])
lock = asyncio.Lock() # Only one _check_attendance_cache() can run at a time
writable = asyncio.Event() # If cleared blocks insert operations until setted back
writable.set()

if CONNECT_DB:
    _conn = sqlite3.connect('cache.db')
    _conn.execute("""CREATE TABLE IF NOT EXISTS attendance_input_fields (
                        timetable_id INTEGER PRIMARY KEY,
                        starttime TEXT,
                        endtime TEXT,
                        class_date TEXT,
                        class_id INTEGER
                    );""")
    @atexit.register
    def _close_db_connection():
        _conn.commit()
        _conn.close()

def default_internal_connector(func):
    """Implements a decorator that checks if a connector isn't given for a
    decorated coroutine. When connector is None, it assigns a connector, and
    closes it after the coroutine ends."""
    @functools.wraps(func)
    async def wrapped(*args, **kwargs):
        internal_connector = False
        if kwargs.get('connector', None) is None:
            internal_connector = True
            connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS)
            kwargs['connector'] = connector
        try:
            return await func(*args, **kwargs)
        finally:
            if internal_connector:
                await connector.close()
    return wrapped

@default_internal_connector
async def _check_attendance_cache(*, connector=None):
    if CONNECT_DB:
        async def compare(row, session): # True if same else False
            f = await _req_input_fields(row[0], session, cache_into_db=False)
            try:
                return True if row[0] == f.timetable_id and row[3] == f.class_date and row[4] == f.class_id else False
            except AttributeError:
                return False
        async with lock:
            try:
                writable.clear()
                await asyncio.sleep(0.2)
                rows = _conn.execute(f"""SELECT * FROM attendance_input_fields ORDER BY timetable_id LIMIT 3;""").fetchall()
                connector = connector or global_connector
                async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
                    tasks = [asyncio.create_task(compare(row, session)) for row in rows]
                    resp = await _request('GET', 'https://mmls.mmu.edu.my/', session=session)
                    mmls_status = resp.status
                    results = await asyncio.gather(*tasks)
                if mmls_status != 200:
                    raise MMLSResponseError(f'Response status not OK. Received {mmls_status}.', mmls_status)
                outdated = not next((False for result in results if result == False), True)
                if outdated:
                    _conn.execute("""DELETE * FROM attendance_input_fields;""") #purges cache. Need to make sure protected against network errors.
            finally:
                writable.set()

async def _request(method, url, *, data=None, params=None, headers=None, cookies=None, session=None):
    """An internal function that wraps aiohttp.request to enable timeout, retries,
    and retry backoff time, and max concurrent connections."""
    internal_session = False
    if not session:
        internal_session = True
        connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS)
        session = aiohttp.ClientSession(connector=connector)
    timeout = aiohttp.ClientTimeout(sock_connect=NETWORK_TIMEOUT, sock_read=NETWORK_TIMEOUT)
    try:
        for i in range(NETWORK_RETRIES+1):
            try:
                return await session.request(method, url, data=data, params=params, headers=headers,
                                             cookies=cookies, timeout=timeout)
            except asyncio.TimeoutError:
                if i == NETWORK_RETRIES:
                    raise
                await asyncio.sleep(NETWORK_RETRY_BACKOFF)
    finally:
        if internal_session:
            await session.close()

async def _req_input_fields(timetable_id, session, *, cache_into_db=True):
    resp = await _request('GET', f"https://mmls.mmu.edu.my/attendance:0:0:{timetable_id}", session=session)
    try:
        if resp.status == 500:
            return None
        if resp.status != 200:
            raise MMLSResponseError(f'Response status not OK. Received {resp.status}.', resp.status)
        tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
    finally:
        await resp.release()
    f = InputFields(
        timetable_id = timetable_id,
        start_time = tree.xpath("//input[@name='starttime']/@value")[0],
        end_time = tree.xpath("//input[@name='endtime']/@value")[0],
        class_date = tree.xpath("//input[@name='class_date']/@value")[0],
        class_id = int(tree.xpath("//input[@name='class_id']/@value")[0])
    )
    if CONNECT_DB and cache_into_db:
        try:
            await writable.wait()
            _conn.execute("""INSERT INTO attendance_input_fields
                (timetable_id, starttime, endtime, class_date, class_id)
                VALUES (?, ?, ?, ?, ?)""",
                (f.timetable_id, f.start_time, f.end_time, f.class_date, f.class_id))
        except sqlite3.IntegrityError:
            pass
    return f

@default_internal_connector
async def load_online(SubjectDB_obj, user_id, password, *, connector=None):
    """Loads registered subjects and all classes in those subjects into a SubjectDB
    object. It needs student ID and password to parse subjects and classes. If
    credentials are incorrect, it returns False, but True if it succeeds."""
    connector = connector or global_connector
    async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
        SubjectDB_obj_tmp = SubjectDB()
        try:
            resp = await _request('GET', 'https://mmls.mmu.edu.my/', session=session)
            if resp.status != 200:
                raise MMLSResponseError(f'Response status not OK. Received {resp.status}.', resp.status)
            tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
        finally:
            await resp.release()
        token = tree.xpath("//input[@name='_token']/@value")[0]
        # ===== Log in to MMLS =====
        data = {'stud_id' : user_id, 'stud_pswrd' : password, '_token' : token}
        try:
            resp = await _request('POST', 'https://mmls.mmu.edu.my/checklogin', data=data, session=session)
            if resp.status == 500:
                return False
            if resp.status != 200:
                raise MMLSResponseError(f'Response status not OK. Received {resp.status}.', resp.status)
        # ===== Parse subjects =====
            tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
        finally:
            await resp.release()
        SUBJECT_XPATH = "//div[@class='list-group ' and @style='margin-top:-15px']/span/a[1]"
        names = [name.split(' - ') for name in tree.xpath(f"{SUBJECT_XPATH}/text()")]
        links = [link[24:].split(':') for link in tree.xpath(f"{SUBJECT_XPATH}/@href")]
        names_and_links = [[data for nested_list in zipped for data in nested_list]
                           for zipped in zip(names, links)]
        for code, name, sid, coid in names_and_links:
            SubjectDB_obj_tmp.add_subject(int(sid), code=code, name=name, coordinator_id=int(coid))
        # ===== Parse classes =====
        async def parse_classes(subject, session):
            sid, coid = subject.id, subject.coordinator_id
            class_list_url = f"https://mmls.mmu.edu.my/studentlist:{sid}:{coid}:0"
            try:
                resp = await _request('GET', class_list_url, session=session)
                if resp.status != 200:
                    raise MMLSResponseError(f'Response status not OK. Received {resp.status}.', resp.status)
                tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
            finally:
                await resp.release()
            cls_xpath = "//select[@id='select_class']/*[not(self::option[@value='0'])]"
            class_ids = tree.xpath(f"{cls_xpath}/@value")
            class_codes = tree.xpath(f"{cls_xpath}/text()")
            for cid, code in zip(class_ids, class_codes):
                subject.add_class(int(cid), code=code)
        tasks = [asyncio.create_task(
                parse_classes(subject, session)
                ) for subject in SubjectDB_obj_tmp.subjects]
        await asyncio.wait(tasks)
        SubjectDB_obj.update(SubjectDB_obj_tmp)
        try:
            resp = await _request('GET', 'https://mmls.mmu.edu.my/logout', session=session)
            if resp.status != 200:
                raise MMLSResponseError(f'Response status not OK. Received {resp.status}.', resp.status)
        finally:
            await resp.release()
        return True

@default_internal_connector
async def autoselect_classes(SubjectDB_obj, user_id, *, connector=None):
    """Autoselects classes in a SubjectDB object that the user, with the given
    student ID, has registered for in the current trimester."""
    connector = connector or global_connector
    async def select_if_registered(user_id, kelas, connector):
        not_reg_xpath = "//div[@class='alert alert-danger']/text()='You are not register to this class.'"
        async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
            #needs standalone sessions if done concurrently but they can share the same connector
            data = {'class_id': kelas.id, 'stud_id': user_id, 'stud_pswrd': '0'}
            headers = {'Referer': 'https://mmls.mmu.edu.my/attendance:0:0:1'}
            resp = await _request('POST', 'https://mmls.mmu.edu.my/attendancelogin',
                                 data=data, headers=headers, session=session)
            if resp.status != 200:
                raise MMLSResponseError(f'Response status not OK. Received {resp.status}.', resp.status)
            tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
            if not tree.xpath(not_reg_xpath):
                kelas.selected = True
    tasks = [asyncio.create_task(
            select_if_registered(user_id, kelas, connector)
            ) for kelas in SubjectDB_obj.classes]
    await asyncio.wait(tasks)

@default_internal_connector
async def scrape(SubjectDB_obj, start_timetable_id, end_timetable_id, *, queue=None, connector=None):
    """Searches for timetable ID belonging to any selected class in the instance's
    loaded SubjectDB object given a range of timetable ID. Returns a list of
    ScrapedTimetable objects. If an asyncio.Queue() object is provided, it
    queues resultant ScrapedTimetable objects in that instead.""" # TODO: Update docstring
    connector = connector or global_connector
    scraped_timetables = []
    pending_ttids = [ttid for ttid in range(start_timetable_id, end_timetable_id+1)]
    class_id_to_class_obj = {kelas.id: kelas for kelas in SubjectDB_obj.selected_classes}
    if CONNECT_DB:
        await _check_attendance_cache(connector=connector)
        cursor = _conn.execute(f"""SELECT * FROM attendance_input_fields
            WHERE timetable_id >= {start_timetable_id} AND timetable_id <= {end_timetable_id};""")
        for row in cursor.fetchall():
            if row[4] in class_id_to_class_obj:
                kelas = class_id_to_class_obj[row[4]]
                scraped_timetable = ScrapedTimetable(
                    timetable_id = row[0],
                    start_time = row[1],
                    end_time = row[2],
                    class_date = row[3],
                    class_id = row[4],
                    class_code = kelas.code,
                    subject_code = kelas.subject.code,
                    subject_name = kelas.subject.name,
                    subject_id = kelas.subject.id,
                    coordinator_id = kelas.subject.coordinator_id
                )
                if queue is None:
                    scraped_timetables.append(scraped_timetable)
                else:
                    queue.put_nowait(scraped_timetable)
            pending_ttids.remove(row[0])
    task_queue = asyncio.Queue(500)
    async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
        try:
            async def feeder(pending_ttids, session, task_queue):
                try:
                    for timetable_id in pending_ttids:
                        task = asyncio.create_task(_req_input_fields(timetable_id, session))
                        await task_queue.put(task)
                except asyncio.CancelledError:
                    task.cancel()
            feeder_task = asyncio.create_task(feeder(pending_ttids, session, task_queue))
            invalid_counter = 25 # hope that total error 500 holes in the attendance url range does not exceed this value
            for _ in pending_ttids:
                if not invalid_counter:
                    break
                task = await task_queue.get()
                inp_fields = await task
                try:
                    if inp_fields.class_id in class_id_to_class_obj:
                        kelas = class_id_to_class_obj[inp_fields.class_id]
                        scraped_timetable = ScrapedTimetable(
                            timetable_id = inp_fields.timetable_id,
                            start_time = inp_fields.start_time,
                            end_time = inp_fields.end_time,
                            class_date = inp_fields.class_date,
                            class_id = inp_fields.class_id,
                            class_code = kelas.code,
                            subject_code = kelas.subject.code,
                            subject_name = kelas.subject.name,
                            subject_id = kelas.subject.id,
                            coordinator_id = kelas.subject.coordinator_id
                            )
                        if queue is None:
                            scraped_timetables.append(scraped_timetable)
                        else:
                            queue.put_nowait(scraped_timetable)
                    invalid_counter = 25
                except AttributeError:
                    invalid_counter -= 1
                task_queue.task_done()
        finally:
            feeder_task.cancel()
            await asyncio.gather(feeder_task, return_exceptions=True)
            while not task_queue.empty():
                task = task_queue.get_nowait()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                task_queue.task_done()
    return scraped_timetables

@default_internal_connector
async def scrape_date(SubjectDB_obj, start_date, end_date, *, queue=None, connector=None):
    """Wraps scrape() and date_to_timetable(). Gets the first or the last
    timetable ID with the input dates, then uses the timetable IDs as the
    range of scrape(). Returns a list of ScrapedTimetable objects. If an
    asyncio.Queue() object is provided, it queues resultant ScrapedTimetable
    objects in that instead.""" # TODO: Update docstring
    connector = connector or global_connector
    cached_ttids = set()
    scraped_timetables = []
    class_id_to_class_obj = {kelas.id: kelas for kelas in SubjectDB_obj.selected_classes}
    if CONNECT_DB:
        await _check_attendance_cache(connector=connector)
        cursor = _conn.execute(f"""SELECT * FROM attendance_input_fields
            WHERE class_date >= ? AND class_date <= ?;""",
            (start_date.isoformat(), end_date.isoformat()))
        for row in cursor.fetchall():
            cached_ttids.add(row[0])
            if row[4] in class_id_to_class_obj:
                kelas = class_id_to_class_obj[row[4]]
                scraped_timetable = ScrapedTimetable(
                    timetable_id = row[0],
                    start_time = row[1],
                    end_time = row[2],
                    class_date = row[3],
                    class_id = row[4],
                    class_code = kelas.code,
                    subject_code = kelas.subject.code,
                    subject_name = kelas.subject.name,
                    subject_id = kelas.subject.id,
                    coordinator_id = kelas.subject.coordinator_id
                )
                if queue is None:
                    scraped_timetables.append(scraped_timetable)
                else:
                    queue.put_nowait(scraped_timetable)
    start_ttid, end_ttid = await dates_to_timetable_ids(start_date, end_date, connector=connector)
    if start_ttid is None or end_ttid is None:
        return None if not scraped_timetables else scraped_timetables
    if CONNECT_DB:
        for row in _conn.execute("""SELECT timetable_id FROM attendance_input_fields
            WHERE timetable_id >= ? AND timetable_id <= ?;""", (start_ttid, end_ttid)).fetchall():
            cached_ttids.add(row[0])
    pending_ttids = [ttid for ttid in range(start_ttid, end_ttid+1) if ttid not in cached_ttids]
    task_queue = asyncio.Queue(500)
    async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
        try:
            async def feeder(pending_ttids, session, task_queue):
                try:
                    for timetable_id in pending_ttids:
                        task = asyncio.create_task(_req_input_fields(timetable_id, session))
                        await task_queue.put(task)
                except asyncio.CancelledError:
                    task.cancel()
            feeder_task = asyncio.create_task(feeder(pending_ttids, session, task_queue))
            invalid_counter = 25 # hope that total error 500 holes in the attendance url range does not exceed this value
            for _ in pending_ttids:
                if not invalid_counter:
                    break
                task = await task_queue.get()
                inp_fields = await task
                try:
                    if inp_fields.class_id in class_id_to_class_obj:
                        kelas = class_id_to_class_obj[inp_fields.class_id]
                        scraped_timetable = ScrapedTimetable(
                            timetable_id = inp_fields.timetable_id,
                            start_time = inp_fields.start_time,
                            end_time = inp_fields.end_time,
                            class_date = inp_fields.class_date,
                            class_id = inp_fields.class_id,
                            class_code = kelas.code,
                            subject_code = kelas.subject.code,
                            subject_name = kelas.subject.name,
                            subject_id = kelas.subject.id,
                            coordinator_id = kelas.subject.coordinator_id
                            )
                        if queue is None:
                            scraped_timetables.append(scraped_timetable)
                        else:
                            queue.put_nowait(scraped_timetable)
                    invalid_counter = 25
                except AttributeError:
                    invalid_counter -= 1
                task_queue.task_done()
        finally:
            feeder_task.cancel()
            await asyncio.gather(feeder_task, return_exceptions=True)
            while not task_queue.empty():
                task = task_queue.get_nowait()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                task_queue.task_done()
    return scraped_timetables

@default_internal_connector
async def dates_to_timetable_ids(start_date, end_date, *, converge=True, connector=None):
    """Returns a range of timetable IDs by doing a binary search on a range of
    timetable IDs for either the first occurence or the last occurence of a given
    date."""
    connector = connector or global_connector
    cache = {}
    async def cached_request(ttid, session):
        if ttid not in cache:
            cache[ttid] = await _request('GET', f"https://mmls.mmu.edu.my/attendance:0:0:{ttid}", session=session)
        return cache[ttid]
    async def date_to_ttid(date, session, *, first_occurrence=True):
        ubound, lbound = MAX_TIMETABLE_ID, MIN_TIMETABLE_ID
        while True: #Option: 1 for first occurence, -1 for last occurence.
            curr_ttid = (ubound+lbound)//2
            resp = await cached_request(curr_ttid, session)
            if resp.status == 500:
                ubound = curr_ttid-1
                continue
            if resp.status != 200:
                raise MMLSResponseError(f'Response status not OK. Received {resp.status}.', resp.status)
            tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
            curr_date = date.fromisoformat(tree.xpath("//input[@name='class_date']/@value")[0])
            if (date - curr_date).days > 0:
                lbound = curr_ttid+1
            elif (date - curr_date).days < 0:
                ubound = curr_ttid-1
            elif first_occurrence:
                resp = await cached_request(curr_ttid-1, session)
                if resp.status != 200:
                    raise MMLSResponseError(f'Response status not OK. Received {resp.status}.', resp.status)
                tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
                if date.fromisoformat(tree.xpath("//input[@name='class_date']/@value")[0]) != curr_date:
                    return curr_ttid
                ubound = curr_ttid-1
            else:
                resp = await cached_request(curr_ttid+1, session)
                if resp.status == 500:
                    return curr_ttid
                if resp.status != 200:
                    raise MMLSResponseError(f'Response status not OK. Received {resp.status}.', resp.status)
                tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
                if date.fromisoformat(tree.xpath("//input[@name='class_date']/@value")[0]) != curr_date:
                    return curr_ttid
                lbound = curr_ttid+1
            if ubound < lbound:
                return None
    async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
        start_ttid, end_ttid = None, None
        while start_ttid is None or end_ttid is None:
            if start_ttid is None:
                start_ttid_task = asyncio.create_task(date_to_ttid(start_date, session, first_occurrence=True))
            if end_ttid is None:
                end_ttid_task = asyncio.create_task(date_to_ttid(end_date, session, first_occurrence=False))
            start_ttid = start_ttid or await start_ttid_task
            end_ttid = end_ttid or await end_ttid_task
            if not converge:
                break
            if start_ttid is None:
                start_date += timedelta(days=1)
            if end_ttid is None:
                end_date -= timedelta(days=1)
            if start_date > end_date:
                break
        return start_ttid, end_ttid

@default_internal_connector
async def cache_timetable_ids(start_ttid = None, end_ttid = None, *, connector=None):
    if CONNECT_DB:
        await _check_attendance_cache(connector=connector)
        cursor = _conn.execute("SELECT timetable_id FROM attendance_input_fields;")
        cached_ttids = set(row[0] for row in cursor.fetchall())
        cursor.close()
        pending_ttids = [ttid for ttid in range(start_ttid or MIN_TIMETABLE_ID, (end_ttid or MAX_TIMETABLE_ID)+1) if ttid not in cached_ttids]
        task_queue = asyncio.Queue(500)
        async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
            try:
                async def feeder(pending_ttids, session, task_queue):
                    try:
                        for timetable_id in pending_ttids:
                            task = asyncio.create_task(_req_input_fields(timetable_id, session))
                            await task_queue.put(task)
                    except asyncio.CancelledError:
                        task.cancel()
                feeder_task = asyncio.create_task(feeder(pending_ttids, session, task_queue))
                invalid_counter = 25 # hope that total error 500 holes in the attendance url range does not exceed this value
                for _ in pending_ttids:
                    if not invalid_counter:
                        break
                    task = await task_queue.get()
                    result = await task
                    if result is None:
                        invalid_counter -= 1
                    else:
                        invalid_counter = 25
                    task_queue.task_done()
            finally:
                feeder_task.cancel()
                await asyncio.gather(feeder_task, return_exceptions=True)
                while not task_queue.empty():
                    task = await task_queue.get()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    task_queue.task_done()
        return total_cached_timetable_ids()
    return None

def total_cached_timetable_ids():
    if CONNECT_DB:
        return _conn.execute("SELECT COUNT(timetable_id) FROM attendance_input_fields;").fetchone()[0]
    return None

class SubjectDB:
    """A container for subjects and classes with built-in methods."""
    class Subject:
        class Class:
            def __init__(self, Subject_obj, cid, *, code = None, selected = False):
                self.id = cid
                self.code = code
                self.selected = selected
                self._Subject = Subject_obj

            @property
            def subject(self):
                return self._Subject

            def remove(self):
                try:
                    self._Subject._classes.remove(self)
                except ValueError:
                    pass

        def __init__(self, SubjectDB_obj, sid, *, code = None, name = None, coordinator_id = None):
            self.id = sid
            self.code = code
            self.name = name
            self.coordinator_id = coordinator_id
            self._classes = []
            self._SubjectDB = SubjectDB_obj

        def add_class(self, cid, code = None, selected = False):
            """Adds a class (Class object) to an internal list. Checks if there is
            an already a class with the same class ID. If so, it replaces that
            class with the added one."""
            temp_class = self.Class(self, cid, code = code, selected = selected)
            for idx, kelas in enumerate(self._classes):
                if kelas.id == temp_class.id:
                    self._classes[idx] = temp_class
                    return
            self._classes.append(temp_class)

        def remove(self):
            try:
                self._SubjectDB._subjects.remove(self)
            except ValueError:
                pass

        @property
        def classes(self):
            return self._classes

        @property
        def selected_classes(self):
            return [kelas for kelas in _classes if kelas.selected]

    def __init__(self):
        self._subjects = []

    def add_subject(self, sid, *, code=None, name=None, coordinator_id=None):
        """Adds a subject (Subject object) to an internal list. Checks if there is
        an already a subject with the same subject ID. If so, it replaces that
        subject with the added one."""
        t_subject = self.Subject(self, sid, code=code, name=name, coordinator_id=coordinator_id)
        for idx, subject in enumerate(self._subjects):
            if subject.id == sid:
                self._subjects[idx] = t_subject
                return
        self._subjects.append(t_subject)

    def update(self, subjectdb_obj):
        """Updates subjects from a SubjectDB object. Checks for subjects with the
        same subject ID. If there is, it replaces it with the subject from given
        SubjectDB object."""
        subjects = copy.deepcopy(subjectdb_obj.subjects)
        for idx, subject in enumerate(self._subjects):
            for t_idx, t_subject in enumerate(subjects):
                if subject.id == t_subject.id:
                    t_subject._SubjectDB = self
                    self._subjects[idx] = t_subject
                    del subjects[t_idx]
        self._subjects.extend(subjects)

    def get_class(self, class_id):
        return next((kelas for kelas in self.classes if kelas.id == class_id), None)

    @property
    def subjects(self):
        return self._subjects

    @property
    def classes(self):
        return [kelas for subject in self._subjects for kelas in subject._classes]

    @property
    def selected_classes(self):
        return [kelas for subject in self._subjects for kelas in subject._classes if kelas.selected]

    def json(self):
        """Return subjects and classes in json-formatted str
        representation."""
        subjects = [{
            'id': subject.id,
            'code': subject.code,
            'name': subject.name,
            'coordinator_id': subject.coordinator_id,
            'classes': [{
                'id': kelas.id,
                'code': kelas.code,
                'selected': kelas.selected
                } for kelas in subject.classes]
            } for subject in self.subjects]
        return ijson.dumps(subjects)

    def load_json(self, json_str):
        """Replaces subjects and classes stored in the SubjectDB
        object with the ones in the subject_db json-formatted str.
        Useful to load from local file after json.loads()."""
        subjects = ijson.loads(json_str)
        temp_SubjectDB_obj = self.__class__()
        for subject_idx, subject in enumerate(subjects):
            temp_SubjectDB_obj.add_subject(
                sid = subject['id'],
                code = subject['code'],
                name = subject['name'],
                coordinator_id = subject['coordinator_id']
                )
            for kelas in subject['classes']:
                temp_SubjectDB_obj.subjects[subject_idx].add_class(
                    cid = kelas['id'],
                    code = kelas['code'],
                    selected = kelas['selected']
                    )
        self._subjects = []
        self.update(temp_SubjectDB_obj)

    def update_json(self, json_str):
        """Adds subjects and classes encoded in the SubjectDB
        json-formatted str to the SubjectDB object. Useful to
        load from local file after json.loads()"""
        subjects = ijson.loads(json_str)
        temp_SubjectDB_obj = self.__class__()
        for subject_idx, subject in enumerate(subjects):
            temp_SubjectDB_obj.add_subject(
                sid = int(subject['id']),
                code = subject['code'],
                name = subject['name'],
                coordinator_id = int(subject['coordinator_id'])
                )
            for kelas in subject['classes']:
                temp_SubjectDB_obj.subjects[subject_idx].add_class(
                    cid = int(kelas['id']),
                    code = kelas['code'],
                    selected = kelas['selected']
                    )
        self.update(temp_SubjectDB_obj)

class InputFields():
    """A container used to store input field values from an attendance url with
    the timetable ID."""
    __slots__ = 'timetable_id', 'start_time', 'end_time', 'class_date', 'class_id'

    def __init__(self, timetable_id, start_time, end_time, class_date, class_id):
        self.timetable_id = timetable_id
        self.start_time = start_time
        self.end_time = end_time
        self.class_date = class_date
        self.class_id = class_id

class ScrapedTimetable:
    """A container used to store information about a class session, and with that
    information, it can also be used to create attendance URLs and attendance list
    URLs."""

    def __init__(self, *, timetable_id, start_time, end_time, class_date, class_id,
                 class_code = None, coordinator_id = None, subject_id = None,
                 subject_code = None, subject_name = None):
        self.timetable_id = timetable_id
        self.start_time = start_time
        self.end_time = end_time
        self.class_date = class_date
        self.class_id = class_id
        self.class_code = class_code
        self.coordinator_id = coordinator_id
        self.subject_id = subject_id
        self.subject_code = subject_code
        self.subject_name = subject_name

    @property
    def attendance_url(self):
        if self.timetable_id:
            return f"https://mmls.mmu.edu.my/attendance:{self.subject_id}:{self.coordinator_id}:{self.timetable_id}"
        return None

    @property
    def attendance_list_url(self):
        if self.subject_id and self.coordinator_id and self.timetable_id and self.class_id:
            return f"https://mmls.mmu.edu.my/viewAttendance:{self.subject_id}:{self.coordinator_id}:{self.timetable_id}:{self.class_id}:1"
        return None
