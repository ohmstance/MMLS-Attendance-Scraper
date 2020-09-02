from datetime import date, timedelta
from io import StringIO
from lxml import etree
import aiohttp
import asyncio

MAX_CONCURRENT_REQUESTS = 6
NETWORK_TIMEOUT = 15
NETWORK_RETRIES = 3
NETWORK_RETRY_BACKOFF = 3

async def _request(method, url, *, data = None, params = None, headers = None, cookies = None,
                   session = None, semaphore = None):
    """An internal function that wraps aiohttp.request to enable timeout, retries,
    and retry backoff time, while also making use of asyncio.Semaphore() to throttle
    concurrent requests."""
    if session is None:
        session = aiohttp.ClientSession()
    if semaphore is None:
        semaphore = asyncio.Semaphore()
    timeout = aiohttp.ClientTimeout(total=NETWORK_TIMEOUT)
    async with semaphore:
        for _ in range(NETWORK_RETRIES):
            try:
                return await session.request(method, url, data=data, params=params, headers=headers,
                                             cookies=cookies, timeout=timeout)
            except asyncio.TimeoutError:
                await asyncio.sleep(NETWORK_RETRY_BACKOFF)
        return await session.request(method, url, data=data, params=params, headers=headers,
                                     cookies=cookies, timeout=timeout)

async def load_online(SubjectDB_obj, user_id, password):
    """Loads registered subjects and all classes in those subjects into a SubjectDB
    object. It needs student ID and password to parse subjects and classes. If
    credentials are incorrect, it returns False, but True if it succeeds."""
    async with aiohttp.ClientSession() as session:
        subjectdb = SubjectDB()
        response = await _request('GET', 'https://mmls.mmu.edu.my/', session = session)
        html_etree = etree.parse(StringIO(await response.text()), etree.HTMLParser())
        cookie = response.cookies
        token = html_etree.xpath("//input[@name='_token']/@value")[0]
        # ===== Log in to MMLS =====
        data = {'stud_id' : user_id, 'stud_pswrd' : password, '_token' : token}
        response = await _request('POST', 'https://mmls.mmu.edu.my/checklogin', data=data, session = session)
        if response.status == 500:
            return False
        # ===== Parse subjects =====
        html_tree = etree.parse(StringIO(await response.text()), etree.HTMLParser())
        SUBJECT_XPATH = "//div[@class='list-group ' and @style='margin-top:-15px']/span/a[1]"
        names = [name.split(' - ') for name in html_tree.xpath(f"{SUBJECT_XPATH}/text()")]
        links = [link[24:].split(':') for link in html_tree.xpath(f"{SUBJECT_XPATH}/@href")]
        names_and_links = [[data for nested_list in zipped for data in nested_list]
                            for zipped in zip(names, links)]
        for code, name, sid, coid in names_and_links:
            subjectdb.add_subject(int(sid), code = code, name = name, coordinator_id = int(coid))
        # ===== Parse classes =====
        async def parse_classes(subject, session, sem):
            sid, coid = subject.id, subject.coordinator_id
            class_list_url = f"https://mmls.mmu.edu.my/studentlist:{sid}:{coid}:0"
            response = await _request('GET', class_list_url, session = session, semaphore = sem)
            html_etree = etree.parse(StringIO(await response.text()), etree.HTMLParser())
            CLASS_XPATH = "//select[@id='select_class']/*[not(self::option[@value='0'])]"
            class_ids = html_etree.xpath(f"{CLASS_XPATH}/@value")
            class_codes = html_etree.xpath(f"{CLASS_XPATH}/text()")
            for cid, code in zip(class_ids, class_codes):
                subject.add_class(int(cid), code = code)
        sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        tasks = [asyncio.create_task(
                parse_classes(subject, session, sem)
                ) for subject in subjectdb.subjects]
        await asyncio.gather(*tasks, return_exceptions = True)
        SubjectDB_obj.update(subjectdb)
        await _request('GET', 'https://mmls.mmu.edu.my/logout', session = session)
        return True

async def autoselect(SubjectDB_obj, user_id):
    """Autoselects classes in a SubjectDB object that the user, with the given
    student ID, has registered for in the current trimester."""
    async def select_if_registered(user_id, class_queue, sem):
        NOT_REG_XPATH = "//div[@class='alert alert-danger']/text()='You are not register to this class.'"
        async with aiohttp.ClientSession() as session:
            await _request('GET', 'https://mmls.mmu.edu.my/attendance:0:0:1', session = session)
            while True:
                async with sem:
                    kelas = await class_queue.get()
                    data = {'class_id': kelas.id, 'stud_id': user_id, 'stud_pswrd': '0'}
                    headers = {'Referer': 'https://mmls.mmu.edu.my/attendance:0:0:1'}
                    response = await _request('POST', 'https://mmls.mmu.edu.my/attendancelogin',
                                              data = data, headers = headers, session = session)
                    html_etree = etree.parse(StringIO(await response.text()), etree.HTMLParser())
                    if not html_etree.xpath(NOT_REG_XPATH):
                        kelas.selected = True
                    class_queue.task_done()
    class_queue = asyncio.Queue()
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    tasks = [asyncio.create_task(
            select_if_registered(user_id, class_queue, sem)
            ) for _ in range(MAX_CONCURRENT_REQUESTS)]
    for kelas in SubjectDB_obj.classes:
        class_queue.put_nowait(kelas)
    await class_queue.join()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

class Scraper:
    MAX_TIMETABLE_ID = 99_999
    MIN_TIMETABLE_ID = 1
    # _form_fields_cache = [None]*(MAX_TIMETABLE_ID+MIN_TIMETABLE_ID)

    def __init__(self, SubjectDB_obj):
        self._SubjectDB = SubjectDB_obj

    # async def _fetch_attendance_url_fields(self, timetable_id, session = None, semaphore = None):
    #     if isinstance(self.__class__._form_fields_cache[timetable_id], dict):
    #         return self.__class__._form_fields_cache[timetable_id]
    #     else:
    #         response = await _request('GET', f"https://mmls.mmu.edu.my/attendance:0:0:{timetable_id}",
    #                                   session = session, semaphore = semaphore)
    #         if response.status == 500:
    #             return None
    #         html_etree = etree.parse(StringIO(await response.text() ), etree.HTMLParser())
    #         form_fields = {
    #             'timetable_id': timetable_id,
    #             'starttime': html_etree.xpath("//input[@name='starttime']/@value")[0][:-3],
    #             'endtime': html_etree.xpath("//input[@name='endtime']/@value")[0][:-3],
    #             'class_date': html_etree.xpath("//input[@name='class_date']/@value")[0],
    #             'class_id': int(html_etree.xpath("//input[@name='class_id']/@value")[0]),
    #         }
    #         self.__class__._form_fields_cache[timetable_id] = form_fields
    #         return form_fields

    async def _fetch_attendance_url_fields(self, timetable_id, session = None, semaphore = None):
        """Fetches form fields of attendance URLs of timetable ID and returns a
        dict of those fields. The fields are supposed to be cached, but due to
        memory bloat, it is scrapped until an alternative method could be made."""
        response = await _request('GET', f"https://mmls.mmu.edu.my/attendance:0:0:{timetable_id}",
                                  session = session, semaphore = semaphore)
        if response.status == 500:
            return None
        html_etree = etree.parse(StringIO(await response.text()), etree.HTMLParser())
        return {
            'timetable_id': timetable_id,
            'starttime': html_etree.xpath("//input[@name='starttime']/@value")[0][:-3],
            'endtime': html_etree.xpath("//input[@name='endtime']/@value")[0][:-3],
            'class_date': html_etree.xpath("//input[@name='class_date']/@value")[0],
            'class_id': int(html_etree.xpath("//input[@name='class_id']/@value")[0]),
        }

    async def _date_to_timetable(self, date, option, session):
        """Returns a timetable ID by doing a binary search on a range timetable IDs
        for either the first occurence or the last occurence of a given date.
        option = 1 for first occurence and option = -1 for last occurence."""
        upperbound, lowerbound = self.MAX_TIMETABLE_ID, self.MIN_TIMETABLE_ID
        CLASS_DATE_XPATH = "//input[@name='class_date']/@value"
        while(True): #Option: 1 for first occurence, -1 for last occurence.
            curr_timetable = (upperbound+lowerbound)//2
            form_fields = await self._fetch_attendance_url_fields(curr_timetable, session)
            if form_fields is None:
                upperbound = curr_timetable-1
                continue
            current_date = date.fromisoformat(form_fields['class_date'])
            if (date - current_date).days > 0:
                lowerbound = curr_timetable+1
            elif (date - current_date).days < 0:
                upperbound = curr_timetable-1
            else:
                form_fields = await self._fetch_attendance_url_fields(curr_timetable-option, session)
                if form_fields is None:
                    curr_timetable
                if date.fromisoformat(form_fields['class_date']) != current_date:
                    return curr_timetable
                if option == 1:
                    upperbound = curr_timetable-1
                elif option == -1:
                    lowerbound = curr_timetable+1
            if upperbound < lowerbound:
                return None

    async def scrape_date(self, start_date, end_date, *, queue = None):
        """Wraps scrape() and date_to_timetable(). Gets the first or the last
        timetable ID with the input dates, then uses the timetable IDs as the
        range of scrape(). Returns a list of ScrapedTimetable objects. If an
        asyncio.Queue() object is provided, it queues resultant ScrapedTimetable
        objects in that instead."""
        async with aiohttp.ClientSession() as session:
            start_timetable_id = end_timetable_id = None
            while start_timetable_id is None or end_timetable_id is None:
                if start_timetable_id is None:
                    start_ttid_task = asyncio.create_task(
                                      self._date_to_timetable(start_date, 1, session))
                if end_timetable_id is None:
                    end_ttid_task = asyncio.create_task(
                                    self._date_to_timetable(end_date, -1, session))
                start_timetable_id = await start_ttid_task
                end_timetable_id = await end_ttid_task
                if not start_timetable_id:
                    start_date += timedelta(days=1)
                if not end_timetable_id:
                    end_date -= timedelta(days=1)
                if start_date > end_date:
                    return None
            return await self.scrape(start_timetable_id, end_timetable_id, queue = queue)

    async def scrape(self, start_timetable_id, end_timetable_id, *, queue = None):
        """Searches for timetable ID belonging to any selected class in the instance's
        loaded SubjectDB object given a range of timetable ID. Returns a list of
        ScrapedTimetable objects. If an asyncio.Queue() object is provided, it
        queues resultant ScrapedTimetable objects in that instead."""
        async with aiohttp.ClientSession() as session:
            scraped_timetables = []
            sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
            tasks = [asyncio.create_task(
                     self._fetch_attendance_url_fields(timetable_id, session = session, semaphore = sem))
                     for timetable_id in range(start_timetable_id, end_timetable_id+1)]
            class_id_to_class_obj = {kelas.id: kelas for kelas in self._SubjectDB.selected_classes}
            while tasks:
                form_fields = await tasks.pop(0)
                if form_fields is None:
                    for task in tasks:
                        task.cancel()
                    break
                class_id = form_fields['class_id']
                if class_id in class_id_to_class_obj:
                    kelas = class_id_to_class_obj[class_id]
                    scraped_timetable = ScrapedTimetable(
                        timetable_id = form_fields['timetable_id'],
                        start_time = form_fields['starttime'],
                        end_time = form_fields['endtime'],
                        class_date = form_fields['class_date'],
                        class_id = class_id,
                        class_code = kelas.code,
                        subject_code = kelas.subject.code,
                        subject_name = kelas.subject.name,
                        subject_id = kelas.subject.id,
                        coordinator_id = kelas.subject.coordinator_id
                        )
                    if queue is None:
                        scraped_timetables.append(scraped_timetable)
                    else:
                        await queue.put(scraped_timetable)
            if queue is None:
                return scraped_timetables

class SubjectDB:
    """A container for subjects and classes with built-in methods."""
    class Subject:
        class Class:
            def __init__(self, subject_obj, cid, *, code = None, selected = False):
                self.id = cid
                self.code = code
                self.selected = selected
                self._Subject = subject_obj

            @property
            def subject(self):
                return self._Subject

        def __init__(self, subjectdb_obj, sid, *, code = None, name = None, coordinator_id = None):
            self.id = sid
            self.code = code
            self.name = name
            self.coordinator_id = coordinator_id
            self._classes = []
            self._SubjectDB = subjectdb_obj

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

        @property
        def classes(self):
            return self._classes

        @property
        def selected_classes(self):
            return [kelas for kelas in _classes if kelas.selected]

    def __init__(self):
        self._subjects = []

    def add_subject(self, sid, *, code = None, name = None, coordinator_id = None):
        """Adds a subject (Subject object) to an internal list. Checks if there is
        an already a subject with the same subject ID. If so, it replaces that
        subject with the added one."""
        t_subject = self.Subject(self, sid, code = code, name = name, coordinator_id = coordinator_id)
        for idx, subject in enumerate(self._subjects):
            if subject.id == sid:
                self._subjects[idx] = t_subject
                return
        self._subjects.append(t_subject)

    def update(self, subjectdb_obj):
        """Updates subjects from a SubjectDB object. Checks for subjects with the
        same subject ID. If there is, it replaces it with the subject from given
        SubjectDB object."""
        subjects = subjectdb_obj.subjects
        for idx, subject in enumerate(self._subjects):
            for t_idx, t_subject in enumerate(subjects):
                if subject.id == t_subject.id:
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
            return f"https://mmls.mmu.edu.my/viewAttendance:{self.subject_id}:{self.coordinator_id}:{self.timetable_id}:{self.class_id}"
        return None
