from datetime import date, timedelta
from io import StringIO
from lxml import etree
import aiohttp
import asyncio

MAX_CONCURRENT_REQUESTS = 6
NETWORK_TIMEOUT = 15
NETWORK_RETRIES = 3
NETWORK_RETRY_BACKOFF = 3
MAX_TIMETABLE_ID = 99_999
MIN_TIMETABLE_ID = 1

async def _request(method, url, *, data = None, params = None, headers = None, cookies = None,
                   session = None, semaphore = None):
    """An internal function that wraps aiohttp.request to enable timeout, retries,
    and retry backoff time, while also making use of asyncio.Semaphore() to throttle
    concurrent requests."""
    session = session or aiohttp.ClientSession()
    semaphore = semaphore or asyncio.Semaphore()
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

async def load_online(SubjectDB_obj, user_id, password, *, semaphore = None):
    """Loads registered subjects and all classes in those subjects into a SubjectDB
    object. It needs student ID and password to parse subjects and classes. If
    credentials are incorrect, it returns False, but True if it succeeds."""
    semaphore = semaphore or asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession() as session:
        subjectdb = SubjectDB()
        resp = await _request('GET', 'https://mmls.mmu.edu.my/', session=session)
        tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
        cookie = resp.cookies
        token = tree.xpath("//input[@name='_token']/@value")[0]
        # ===== Log in to MMLS =====
        data = {'stud_id' : user_id, 'stud_pswrd' : password, '_token' : token}
        resp = await _request('POST', 'https://mmls.mmu.edu.my/checklogin', data=data, session=session)
        if resp.status == 500:
            return False
        # ===== Parse subjects =====
        tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
        SUBJECT_XPATH = "//div[@class='list-group ' and @style='margin-top:-15px']/span/a[1]"
        names = [name.split(' - ') for name in tree.xpath(f"{SUBJECT_XPATH}/text()")]
        links = [link[24:].split(':') for link in tree.xpath(f"{SUBJECT_XPATH}/@href")]
        names_and_links = [[data for nested_list in zipped for data in nested_list]
                           for zipped in zip(names, links)]
        for code, name, sid, coid in names_and_links:
            subjectdb.add_subject(int(sid), code=code, name=name, coordinator_id=int(coid))
        # ===== Parse classes =====
        async def parse_classes(subject, session, semaphore):
            sid, coid = subject.id, subject.coordinator_id
            class_list_url = f"https://mmls.mmu.edu.my/studentlist:{sid}:{coid}:0"
            resp = await _request('GET', class_list_url, session=session, semaphore=semaphore)
            tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
            cls_xpath = "//select[@id='select_class']/*[not(self::option[@value='0'])]"
            class_ids = tree.xpath(f"{cls_xpath}/@value")
            class_codes = tree.xpath(f"{cls_xpath}/text()")
            for cid, code in zip(class_ids, class_codes):
                subject.add_class(int(cid), code=code)
        tasks = [asyncio.create_task(
                parse_classes(subject, session, semaphore)
                ) for subject in subjectdb.subjects]
        await asyncio.wait(tasks)
        SubjectDB_obj.update(subjectdb)
        await _request('GET', 'https://mmls.mmu.edu.my/logout', session=session)
        return True

async def autoselect_classes(SubjectDB_obj, user_id, *, semaphore = None):
    """Autoselects classes in a SubjectDB object that the user, with the given
    student ID, has registered for in the current trimester."""
    semaphore = semaphore or asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    async def select_if_registered(user_id, class_queue, semaphore):
        not_reg_xpath = "//div[@class='alert alert-danger']/text()='You are not register to this class.'"
        async with aiohttp.ClientSession() as session:
            await _request('GET', 'https://mmls.mmu.edu.my/attendance:0:0:1', session=session)
            while True:
                async with semaphore:
                    kelas = await class_queue.get()
                    data = {'class_id': kelas.id, 'stud_id': user_id, 'stud_pswrd': '0'}
                    headers = {'Referer': 'https://mmls.mmu.edu.my/attendance:0:0:1'}
                    resp = await _request('POST', 'https://mmls.mmu.edu.my/attendancelogin',
                                         data=data, headers=headers, session=session)
                    tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
                    if not tree.xpath(not_reg_xpath):
                        kelas.selected = True
                    class_queue.task_done()
    class_queue = asyncio.Queue()
    tasks = [asyncio.create_task(
            select_if_registered(user_id, class_queue, semaphore)
            ) for _ in range(semaphore._value)]
    for kelas in SubjectDB_obj.classes:
        class_queue.put_nowait(kelas)
    await class_queue.join()
    for task in tasks:
        task.cancel()
    await asyncio.wait(tasks)

async def scrape(SubjectDB_obj, start_timetable_id, end_timetable_id, *, queue = None, semaphore = None):
    """Searches for timetable ID belonging to any selected class in the instance's
    loaded SubjectDB object given a range of timetable ID. Returns a list of
    ScrapedTimetable objects. If an asyncio.Queue() object is provided, it
    queues resultant ScrapedTimetable objects in that instead."""
    semaphore = semaphore or asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession() as session:
        scraped_timetables = []
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        tasks = [asyncio.create_task(
                _request('GET', f"https://mmls.mmu.edu.my/attendance:0:0:{timetable_id}", 
                        session=session, semaphore=semaphore))
                for timetable_id in range(start_timetable_id, end_timetable_id+1)]
        class_id_to_class_obj = {kelas.id: kelas for kelas in SubjectDB_obj.selected_classes}
        while tasks:
            resp = await tasks.pop(0)
            if resp.status == 500:
                for task in tasks:
                    task.cancel()
                break
            tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
            class_id = int(tree.xpath("//input[@name='class_id']/@value")[0])
            if class_id in class_id_to_class_obj:
                kelas = class_id_to_class_obj[class_id]
                scraped_timetable = ScrapedTimetable(
                    timetable_id = int(tree.xpath("//input[@name='timetable_id']/@value")[0]),
                    start_time = tree.xpath("//input[@name='starttime']/@value")[0][:-3],
                    end_time = tree.xpath("//input[@name='endtime']/@value")[0][:-3],
                    class_date = tree.xpath("//input[@name='class_date']/@value")[0],
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

async def scrape_date(SubjectDB_obj, start_date, end_date, *, queue = None, semaphore = None):
    """Wraps scrape() and date_to_timetable(). Gets the first or the last
    timetable ID with the input dates, then uses the timetable IDs as the
    range of scrape(). Returns a list of ScrapedTimetable objects. If an
    asyncio.Queue() object is provided, it queues resultant ScrapedTimetable
    objects in that instead."""
    semaphore = semaphore or asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession() as session:
        start_ttid = end_ttid = None
        while start_ttid is None or end_ttid is None:
            if start_ttid is None:
                start_ttid_task = asyncio.create_task(
                                  date_to_timetable(start_date, 1, session=session, semaphore=semaphore)
                                  )
            if end_ttid is None:
                end_ttid_task = asyncio.create_task(
                                date_to_timetable(end_date, -1, session=session, semaphore=semaphore)
                                )
            start_ttid = await start_ttid_task
            end_ttid = await end_ttid_task
            if not start_ttid:
                start_date += timedelta(days=1)
            if not end_ttid:
                end_date -= timedelta(days=1)
            if start_date > end_date:
                return None
        return await scrape(SubjectDB_obj, start_ttid, end_ttid, queue=queue, semaphore=semaphore)

async def date_to_timetable(date, option, *, session = None, semaphore = None):
    """Returns a timetable ID by doing a binary search on a range timetable IDs
    for either the first occurence or the last occurence of a given date.
    option = 1 for first occurence and option = -1 for last occurence."""
    if not (option == 1 or option == -1):
        raise ValueError("Option must be 1 or -1")
    ubound, lbound = MAX_TIMETABLE_ID, MIN_TIMETABLE_ID
    semaphore = semaphore or asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    session = session or aiohttp.ClientSession()
    async with semaphore:
        while(True): #Option: 1 for first occurence, -1 for last occurence.
            curr_ttid = (ubound+lbound)//2
            resp = await _request('GET', f"https://mmls.mmu.edu.my/attendance:0:0:{curr_ttid}", 
                                 session=session)
            if resp.status == 500:
                ubound = curr_ttid-1
                continue
            tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
            curr_date = date.fromisoformat(tree.xpath("//input[@name='class_date']/@value")[0])
            if (date - curr_date).days > 0:
                lbound = curr_ttid+1
            elif (date - curr_date).days < 0:
                ubound = curr_ttid-1
            else:
                resp = await _request('GET', f"https://mmls.mmu.edu.my/attendance:0:0:{curr_ttid-option}", 
                                     session=session)
                if resp.status == 500:
                    return curr_ttid
                tree = etree.parse(StringIO(await resp.text()), etree.HTMLParser())
                if date.fromisoformat(tree.xpath("//input[@name='class_date']/@value")[0]) != curr_date:
                    return curr_ttid
                if option == 1:
                    ubound = curr_ttid-1
                elif option == -1:
                    lbound = curr_ttid+1
            if ubound < lbound:
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
