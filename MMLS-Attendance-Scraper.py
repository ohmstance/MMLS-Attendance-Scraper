from datetime import date, datetime, timedelta
from io import StringIO
from lxml import etree
import aiohttp
import asyncio
import cmd
import getpass
import os
import re
import time

BASE_STUDENT_LIST_URL = 'https://mmls.mmu.edu.my/studentlist'
MMLS_LOGIN_URL = 'https://mmls.mmu.edu.my/checklogin' #stud_id, stud_pswrd, _token. POST
MMLS_ATTENDANCE_LOGIN_URL= 'https://mmls.mmu.edu.my/attendancelogin' #stud_id, stud_pswrd, timetable_id, starttime, endtime, class_date, class_id, _token. POST.
MMLS_URL = 'https://mmls.mmu.edu.my/'                               #^^^Need Referer header: any attendance link.
MMLS_LOGOUT_URL = 'https://mmls.mmu.edu.my/logout' #headers: cookie. GET
MOBILE_LOGIN_URL = 'https://mmumobileapps.mmu.edu.my/api/auth/login2' #username, password. POST
MOBILE_SUBJECT_LIST_URL = 'https://mmumobileapps.mmu.edu.my/api/mmls/subject' #token. GET
MOBILE_LOGOUT_URL = 'https://mmumobileapps.mmu.edu.my/api/logout' #token. GET
BASE_ATTENDANCE_URL = 'https://mmls.mmu.edu.my/attendance'
BASE_ATTENDANCE_LIST_URL = 'https://mmls.mmu.edu.my/viewAttendance'
MAX_CONCURRENT_REQUESTS = 6 #Max value: 64 on Windows due to 'Windows selector() API limitations'
NETWORK_TIMEOUT = 15
NETWORK_RETRIES = 3
NETWORK_RETRY_BACKOFF = 3
MAX_TIMETABLE_ID = 99999
MIN_TIMETABLE_ID = 1
PRINT_ATTENDANCE_LIST = False
TIME_IT = False #Times network-dependent tasks. Timed: Login, autoselect, and search.

async def request(method, url, session=None, *, data=None, params=None, headers=None, cookies=None):
    timeout = aiohttp.ClientTimeout(total=NETWORK_TIMEOUT)
    if session is None:
        session = aiohttp.ClientSession()
    for _ in range(NETWORK_RETRIES):
        try:
            return await session.request(method, url, data=data, params=params, headers=headers, cookies=cookies, timeout=timeout)
        except asyncio.TimeoutError:
            await asyncio.sleep(NETWORK_RETRY_BACKOFF)
    return await session.request(method, url, data=data, params=params, headers=headers, cookies=cookies, timeout=timeout)

async def get_attendance_etree(timetable_id, session, sem = None):
    if sem is None: #Accepts timetable_id. Parses attendance HTTP response of input timetable_id. Returns ElementTree object, but None type if failed.
        sem = asyncio.Semaphore(1)
    async with sem:
        response = await request('GET', f"{BASE_ATTENDANCE_URL}:0:0:{timetable_id}", session)
        if response.status == 500:
            return None
        html = StringIO(await response.text())
        return etree.parse(html, etree.HTMLParser())

async def date_to_timetable_id(date, option, session, *, upperbound = MAX_TIMETABLE_ID, lowerbound = MIN_TIMETABLE_ID):
    CLASS_DATE_XPATH = "//input[@name='class_date']/@value"
    while(True): #Option: 1 for first occurence, -1 for last occurence; Binary search algorithm; Returns None if no class on that date.
        current_timetable_id = (upperbound+lowerbound)//2
        html_etree = await get_attendance_etree(current_timetable_id, session)
        if html_etree is None:
            upperbound = current_timetable_id-1
            continue
        current_date = date.fromisoformat(html_etree.xpath(CLASS_DATE_XPATH)[0])
        if (date - current_date).days > 0:
            lowerbound = current_timetable_id+1
        elif (date - current_date).days < 0:
            upperbound = current_timetable_id-1
        else:
            look_ahead_etree = await get_attendance_etree(current_timetable_id-option, session)
            if (look_ahead_etree is None or
                date.fromisoformat(look_ahead_etree.xpath(CLASS_DATE_XPATH)[0]) != current_date):
                return current_timetable_id
            if option == 1:
                upperbound = current_timetable_id-1
            elif option == -1:
                lowerbound = current_timetable_id+1
        if upperbound < lowerbound:
            return None

async def scrape(start_timetable_id, end_timetable_id):
    async with aiohttp.ClientSession() as session:
        tasks = []
        sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        for timetable_id in range(start_timetable_id, end_timetable_id+1):
            task = asyncio.create_task(get_attendance_etree(timetable_id, session, sem))
            tasks.append(task)
        found_attendance_link = False
        while tasks:
            html_etree = await tasks.pop(0)
            if not html_etree:
                for task in tasks:
                    task.cancel()
                del tasks
                break
            parsed_class_id = html_etree.xpath("//input[@name='class_id']/@value")[0]
            if (subjects_db.is_class_in_database(parsed_class_id) and
                subjects_db.is_class_selected(parsed_class_id)):
                print()
                print_links(html_etree)
                found_attendance_link = True
        if not found_attendance_link:
            print('No links from selected classes found.')

def print_subjects(subject_list):
    for subject_no, subject in enumerate(subject_list, 1):
        print(f"{subject_no}. {subject['subject_code']} - {subject['subject_name']}") #1. ECE2056 - DATA COMM AND NEWORK
        for char_id, s_class in enumerate(subject['classes'], ord('a')):
            print(f"   {chr(char_id)}. [{'X' if s_class['selected'] else ' '}] {s_class['class_name']}") #   [X] EC01

def print_links(html_etree):
    classid = html_etree.xpath("//input[@name='class_id']/@value")[0]
    subject = subjects_db.get_subject(classid)
    s_class = subjects_db.get_class(classid)
    date = html_etree.xpath("//input[@name='class_date']/@value")[0]
    starttime = html_etree.xpath("//input[@name='starttime']/@value")[0][:-3]
    endtime = html_etree.xpath("//input[@name='endtime']/@value")[0][:-3]
    subjectcode = subject['subject_code']
    subjectname = subject['subject_name']
    classname = s_class['class_name']
    subjectid = subject['subject_id']
    coordinatorid = subject['coordinator_id']
    timetableid = html_etree.xpath("//input[@name='timetable_id']/@value")[0]
    print(f"[{date} {starttime}-{endtime}] {subjectcode} - {subjectname} ({classname})")
    print(f"{BASE_ATTENDANCE_URL}:{subjectid}:{coordinatorid}:{timetableid}") #subjectID and coor.ID don't matter for attendance links
    if PRINT_ATTENDANCE_LIST:
        print(f"{BASE_ATTENDANCE_LIST_URL}:{subjectid}:{coordinatorid}:{timetableid}:{classid}:1") #Attendance list links requires all IDs to be correct

class SubjectsDB:
    subjects_db = []
    class_id_to_index_dict = {}
    registered_classes = set()
    trimester_start_date = None
    user_id = None
    cookie = None
    token = None
    mobile_token = None

    async def init_mmls(self):
        async with aiohttp.ClientSession() as session:
            response = await request('GET', MMLS_URL, session)
            html = StringIO(await response.text())
            tree = etree.parse(html, etree.HTMLParser())
            self.cookie = response.cookies
            self.token = tree.xpath("//input[@name='_token']/@value")[0]

    async def login(self, user_id, password):
        if not self.token or not self.cookie:
            await self.init_mmls()
        async with aiohttp.ClientSession(cookies=self.cookie, ) as session:
            data = {'stud_id' : user_id, 'stud_pswrd' : password, '_token' : self.token}
            response = await request('POST', MMLS_LOGIN_URL, session, data=data)
            if response.status == 500:
                return False
            self.user_id = user_id
            await self.load_subjects(response, session)
            return True

    async def login_mobile(self, user_id, password):
        async with aiohttp.ClientSession() as session:
            data = {'username' : user_id, 'password' : password}
            response = await request('POST', MOBILE_LOGIN_URL, session, data=data)
            if response.status == 422 or response.status == 500:
                return False
            JSON = await response.json()
            self.mobile_token = JSON['token']
            params = {'token' : self.mobile_token}
            response = await request('GET', MOBILE_SUBJECT_LIST_URL, session, params=params)
            JSON = await response.json()
            self.trimester_start_date = date.fromisoformat(JSON[0]['sem_start_date']) #Get sem_start_date of the first subject in MMU Mobile subject list
        return True

    async def logout(self):
        if self.cookie is not None:
            async with aiohttp.ClientSession(cookies=self.cookie, ) as session:
                await request('GET', MMLS_LOGOUT_URL, session)

    async def logout_mobile(self):
        if self.mobile_token is not None:
            async with aiohttp.ClientSession() as session:
                data = {'token' : self.mobile_token}
                await request('POST', MOBILE_LOGOUT_URL, session, data=data)

    async def load_subjects(self, response = None, session = None):
        if not response or not session:
            session = aiohttp.ClientSession(cookies=self.cookie, )
            response = request('GET', f"{MMLS_URL}home", session)
        html = StringIO(await response.text())
        tree = etree.parse(html, etree.HTMLParser())
        SUBJECT_GROUP_XPATH = "//div[@class='list-group ' and @style='margin-top:-15px']/span/a[1]"
        names = [name.split(' - ') for name in tree.xpath(f"{SUBJECT_GROUP_XPATH}/text()")] # ECE2056 - DATA COMM AND NEWORK
        links = [link[24:].split(':') for link in tree.xpath(f"{SUBJECT_GROUP_XPATH}/@href")] # https://mmls.mmu.edu.my/232:1592795134
        names_and_links = [[data for nested_list in zipped for data in nested_list] for zipped in zip(names, links)]
        temp_subjects_list = [{
            'subject_code': subject_code, #Eg. ECE2056
            'subject_name': subject_name, #Eg. DATA COMM AND NEWORK
            'subject_id': subject_id, #Eg. 332
            'coordinator_id': coordinator_id, #Eg. 1585369691
            'classes' : [] #List of classes in dict, with its class code and select attribute.
        } for subject_code, subject_name, subject_id, coordinator_id in names_and_links]
        async def parse_classes(subject, session, sem):
            async with sem: #Accepts subject dict in SubjectListDB and cookie for MMLS. Returns a list of class dicts.
                subject_id, coordinator_id = subject['subject_id'], subject['coordinator_id']
                subject_student_list_url = f"{BASE_STUDENT_LIST_URL}:{subject_id}:{coordinator_id}:0"
                response = await request('GET', subject_student_list_url, session)
                html = StringIO(await response.text())
                tree = etree.parse(html, etree.HTMLParser())
                GROUP_DROPDOWN_XPATH = "//select[@id='select_class']/*[not(self::option[@value='0'])]"
                class_names = tree.xpath(f"{GROUP_DROPDOWN_XPATH}/text()")
                class_ids = tree.xpath(f"{GROUP_DROPDOWN_XPATH}/@value")
                return [{'class_name' : name, 'class_id' : id, 'selected' : False} for name, id in zip(class_names, class_ids)]
        sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        tasks = [asyncio.create_task(parse_classes(subject, session, sem)) for subject in temp_subjects_list]
        for subject_index, classes in enumerate(tasks):
            temp_subjects_list[subject_index]['classes'] = await classes
        for subject_index, subject in enumerate(self.subjects_db):
            for temp_subject_index, temp_subject in enumerate(temp_subjects_list):
                if temp_subject['subject_id'] == subject['subject_id']:
                    self.subjects_db[subject_index] = temp_subject
                    del temp_subjects_list[temp_subject_index]
        self.subjects_db.extend(temp_subjects_list)
        self.update_hash()

    async def autoselect_classes(self):
        async def is_user_in_class(user_id, class_id_queue, result_list): #Returns True if user in class, False otherwise.
            NOT_REGISTERED_XPATH = "//div[@class='alert alert-danger']/text()='You are not register to this class.'"
            async with aiohttp.ClientSession() as session:
                await request('GET', 'https://mmls.mmu.edu.my/attendance:0:0:1', session)
                while True:
                    class_id = await class_id_queue.get()
                    data = {'class_id' : class_id, 'stud_id' : user_id, 'stud_pswrd' : '0'}
                    headers = {'Referer' : 'https://mmls.mmu.edu.my/attendance:0:0:1'}
                    response = await request('POST', MMLS_ATTENDANCE_LOGIN_URL, session, data=data, headers=headers)
                    html = StringIO(await response.text())
                    tree = etree.parse(html, etree.HTMLParser())
                    result = (class_id, False) if tree.xpath(NOT_REGISTERED_XPATH) else (class_id, True)
                    result_list.append(result)
                    class_id_queue.task_done()
        if self.registered_classes:
            for class_id in self.registered_classes:
                self.get_class(class_id).update({'selected': True})
            return True
        elif self.user_id is not None:
            class_id_queue = asyncio.Queue()
            result_list = []
            tasks = [asyncio.create_task(
                    is_user_in_class(self.user_id, class_id_queue, result_list)
                    ) for _ in range(MAX_CONCURRENT_REQUESTS)]
            for class_id in self.class_id_to_index_dict.keys():
                class_id_queue.put_nowait(class_id)
            await class_id_queue.join()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for class_id, is_in_class in result_list:
                if is_in_class:
                    self.get_class(class_id).update({'selected': True})
                    self.registered_classes.add(class_id)
            return True
        else:
            return False

    def selector(self, option = None, subject_index = None, class_index = None): #True: select, False: deselect, None: toggle
        try:
            if subject_index is None:
                for subject_index, _ in enumerate(self.subjects_db):
                    self.selector(option, subject_index, class_index)
            elif class_index is None:
                for class_index, _ in enumerate(self.subjects_db[subject_index]['classes']):
                    self.selector(option, subject_index, class_index)
            else:
                toggle = not self.subjects_db[subject_index]['classes'][class_index]['selected']
                self.subjects_db[subject_index]['classes'][class_index]['selected'] = toggle if option is None else option
        except IndexError:
            pass

    def update_hash(self):
        self.class_id_to_index_dict = {}
        for subject_index, subject in enumerate(self.subjects_db):
            for class_index, s_class in enumerate(subject['classes']):
                self.class_id_to_index_dict[s_class['class_id']] = {'subject_index': subject_index, 'class_index': class_index}

    def get_subject(self, class_id):
        if str(class_id) in self.class_id_to_index_dict:
            subject_index = self.class_id_to_index_dict[str(class_id)]['subject_index']
            return self.subjects_db[subject_index]
        for subject in self.subjects_db:
            s_class = next((s_class for s_class in subject['classes'] if s_class['class_id'] == class_id), None)
            return subject if s_class else None

    def get_class(self, class_id):
        if str(class_id) in self.class_id_to_index_dict:
            subject_index = self.class_id_to_index_dict[str(class_id)]['subject_index']
            class_index = self.class_id_to_index_dict[str(class_id)]['class_index']
            return self.subjects_db[subject_index]['classes'][class_index]
        for subject in self.subjects_db:
            return next((s_class for s_class in subject['classes'] if s_class['class_id'] == class_id), None)

    def is_class_selected(self, class_id):
        subject_index = self.class_id_to_index_dict[str(class_id)]['subject_index']
        class_index = self.class_id_to_index_dict[str(class_id)]['class_index']
        return self.subjects_db[subject_index]['classes'][class_index]['selected']

    def is_class_in_database(self, class_id):
        return str(class_id) in self.class_id_to_index_dict

    def is_any_class_selected(self):
        return next((True for subject in self.subjects_db for s_class in subject['classes'] if s_class['selected']), False)

class Prompt(cmd.Cmd):
    nohelp = "No help on '%s'.\n"

    def do_login(self, args):
        ("\n"
        "Log in to MMLS and MMU Mobile and load subjects and classes.\n"
        "————————————————————————————————————————————————————————————\n"
        "Syntax:   login [student_id]                                \n")
        async def login_all(user_id, password):
            mobile_task = asyncio.create_task(subjects_db.login_mobile(user_id, password))
            mmls_task = asyncio.create_task(subjects_db.login(user_id, password))
            if not await mobile_task:
                return False
            await mmls_task
            return True
        user_id = args.split()[0] if args else input('Student ID: ')
        password = getpass.getpass()
        init_time = time.time()
        if asyncio.run(login_all(user_id, password)):
            print('Success.\n')
        else:
            print('Wrong student ID or password.\n')
        print(f'Command took {time.time()-init_time:.2f}s\n\n' if TIME_IT else '', end='')

    def do_print(self, args):
        ("\nDisplay stored subjects, classes and selection.\n")
        print_subjects(subjects_db.subjects_db)
        print()

    def do_autoselect(self, args):
        ("\nAuto-select classes that the student has registered for.\n")
        init_time = time.time()
        if asyncio.run(subjects_db.autoselect_classes()):
            print_subjects(subjects_db.subjects_db)
            print()
        else:
            print('Please log in to use this command.\n')
        print(f'Command took {time.time()-init_time:.2f}s\n\n' if TIME_IT else '', end='')

    def do_select(self, args):
        ("\n"
        "Add selection to classes.      \n"
        "———————————————————————————————\n"
        "Examples: select 1a 2c 3 4abc 5\n"
        "          select all           \n")
        if not self.change_selection(args, True):
            print("Invalid command. Enter 'help search' for command help.\n")
            return
        print_subjects(subjects_db.subjects_db)
        print()

    def do_deselect(self, args):
        ("\n"
        "Remove selection in classes.     \n"
        "—————————————————————————————————\n"
        "Examples: deselect 1a 2c 3 4abc 5\n"
        "          deselect all           \n")
        if not self.change_selection(args, False):
            print("Invalid command. Enter 'help search' for command help.\n")
            return
        print_subjects(subjects_db.subjects_db)
        print()

    def do_toggle(self, args):
        ("\n"
        "Toggle selection of classes.   \n"
        "———————————————————————————————\n"
        "Examples: toggle 1a 2c 3 4abc 5\n"
        "          toggle all           \n")
        if not self.change_selection(args, None):
            print("Invalid command. Enter 'help search' for command help.\n")
            return
        print_subjects(subjects_db.subjects_db)
        print()

    def change_selection(self, args, op):
        """Parses selection command arguments, creates a dict of sets -- where
        key is subject index and value is a set of class index -- and iterates
        through each item and its set elements which through it does select,
        deselect or toggle operation to classes at their index."""
        args_list = args.lower().split() # E.g. ['1ab', '2ac', '3', '4a']
        if args_list and args_list[0] == 'all':
            subjects_db.selector(op, None, None)
        else:
            op_dict = {} #op_dict = {subject_index: {class_index, ...}, ...}
            for arg in args_list:
                re_obj = re.match('[0-9]+', arg[0])
                if re_obj is None:
                    continue
                subject_index = int(re_obj[0])-1
                if subject_index < 0:
                    continue
                re_obj = re.search('[a-z]+', arg)
                letters = re_obj[0] if re_obj is not None else ''
                class_choices = [ord(char)-ord('a') for char in letters]
                op_dict[subject_index] = set(class_choices)
            if not op_dict:
                return False
            for subject_index, class_set in op_dict.items():
                if not class_set:
                    subjects_db.selector(op, subject_index, None)
                else:
                    for class_index in class_set:
                        subjects_db.selector(op, subject_index, class_index)
        return True

    def do_search(self, args):
        ("\n"
        "Search for attendance links in a specified range.\n"
        "—————————————————————————————————————————————————\n"
        "Syntax:   search date <start_date> <end_date>    \n"
        "          search date <date>                     \n"
        "          ...if date is empty, uses current date.\n"
        "          search timetable <start_id> <end_id>   \n\n"
        "Examples: search date 2020-04-20 2020-08-31      \n"
        "          search date 2020-07-04                 \n"
        "          search timetable 66666 69420           \n")
        cmd = ''.join(args.split()[:1])
        args = ' '.join(args.split()[1:])
        if not subjects_db.is_any_class_selected():
            print('No classes selected for searching.')
        elif not cmd or not (cmd == 'date' or cmd == 'timetable'):
            print("Invalid command. Enter 'help search' for command help.")
        # =============== search date <start_date> <end_date> ===============
        elif cmd == 'date':
            args_list = args.split()
            try:
                if len(args_list) > 2:
                    print("Too many arguments. Enter 'help search' for command help.\n")
                    return
                elif len(args_list) == 1:
                    start_date = end_date = date.fromisoformat(args_list[0])
                elif len(args_list) == 0:
                    start_date = end_date = (datetime.utcnow()+timedelta(hours=8)).date()
                else:
                    start_date = date.fromisoformat(args_list[0])
                    end_date = date.fromisoformat(args_list[1])
            except ValueError as err:
                print(f"{err}. Use format YYYY-MM-DD.\n")
                return
            tri_start_date = subjects_db.trimester_start_date
            if (tri_start_date is not None and
                (0 <= (start_date - tri_start_date).days <= 2 or 0 <= (start_date - tri_start_date).days <= 2)):
                print("WARNING: Date search is extremely unreliable in the first three trimester days.\n"
                      "         Expect missing or no attendance links. Use timetable search instead!")
            async def scrape_date(start_date, end_date):
                async with aiohttp.ClientSession() as session:
                    start_timetable_id = end_timetable_id = None
                    while True:
                        if start_timetable_id is None:
                            start_ttid_task = asyncio.create_task(date_to_timetable_id(start_date, 1, session))
                        if end_timetable_id is None:
                            end_ttid_task = asyncio.create_task(date_to_timetable_id(end_date, -1, session))
                        start_timetable_id = await start_ttid_task
                        end_timetable_id = await end_ttid_task
                        if not start_timetable_id:
                            start_date += timedelta(days=1)
                        if not end_timetable_id:
                            end_date -= timedelta(days=1)
                        if start_date > end_date:
                            print('No classes found within date range.')
                            break
                        elif start_timetable_id and end_timetable_id:
                            print(f"Searching classes from {start_timetable_id} ({start_date.isoformat()}) to {end_timetable_id} ({end_date.isoformat()}).")
                            await scrape(start_timetable_id, end_timetable_id)
                            break
            init_time = time.time()
            asyncio.run(scrape_date(start_date, end_date))
        # =============== search timetable <start_id> <end_id> ===============
        elif cmd == 'timetable':
            args_list = args.split()
            if not len(args_list) == 2:
                print("Expected two arguments. Enter 'help search' for command help.\n")
                return
            try:
                start_timetable_id = int(args_list[0])
                end_timetable_id = int(args_list[1])
            except ValueError as err:
                print(f"Value error. Enter 'help search' for command help.\n")
                return
            print(f"Searching classes from {start_timetable_id} to {end_timetable_id}.")
            init_time = time.time()
            asyncio.run(scrape(start_timetable_id, end_timetable_id))
        print(f'\nCommand took {time.time()-init_time:.2f}s\n\n' if TIME_IT else '\n', end='')

    def do_exit(self, args):
        ("\nLog out both MMLS and MMU Mobile then terminate this script.\n")
        print('Exiting.')
        async def logout_all():
            asyncio.gather(subjects_db.logout(), subjects_db.logout_mobile(), return_exceptions=True)
        asyncio.run(logout_all())
        exit()

    def do_guided(self, args):
        ("\nStart a guided setup for typical attendance scraping.\n")
        def ask_yes_no(question):
            while True:
                decision = input(f"{question} (y/n): ")
                if (decision.lower() == 'y'): return True
                if (decision.lower() == 'n'): return False
                print("Invalid input.")
        print("How do you want to scrape attendance links?:                           \n"
              "1. Retrieve classes via MMLS login and search by date.*                \n"
              "2. Retrieve classes via MMLS login and search by range of timetable_id.\n\n"
              "*/ Unreliable in the first three trimester days and in some cases.     \n"
              " / If no links were caught use the second option instead.              ")
        while True:
            try:
                what_to_do = int(input('\nChoice: '))
                if not 0 < what_to_do < 3:
                    raise ValueError
                break
            except ValueError:
                print('Invalid input.')
        if subjects_db.user_id is None:
            self.do_login('')
        if subjects_db.user_id is None:
            return
        self.do_print('')
        if ask_yes_no('Auto-select your registered classes?'):
            print()
            self.do_autoselect('')
        if ask_yes_no('Edit class selection?'):
            self.do_print('')
            while True:
                try:
                    subject_index = int(input('Select which subject: '))-1
                    class_indexes = [ord(char)-ord('a') for char in input("Toggle which classes?: ").replace(',', '').split(' ')]
                    for index in class_indexes:
                        subjects_db.selector(None, subject_index, index)
                    self.do_print('')
                except (ValueError, TypeError):
                    print('Invalid input.')
                if not ask_yes_no('Continue editing?'):
                    break
        if what_to_do == 1:
            start_date = input("Search from what date? YYYY-MM-DD: ")
            end_date = input("Until what date? YYYY-MM-DD: ")
            self.do_search(f'date {start_date} {end_date}')
        elif what_to_do == 2:
            start_timetable_id = int(input('Define beginning of timetable_id range: '))
            end_timetable_id = int(input('Define end of timetable_id range: '))
            self.do_search(f'timetable {start_timetable_id} {end_timetable_id}')

    def default(self, line):
        print(f"Command not found: '{line}'. Enter 'help' to list commands.\n")

    def help_help(self):
        print("\n"
        "List available commands or provide command help.\n"
        "————————————————————————————————————————————————\n"
        "Syntax:   help [command]                        \n")

    def help_manual(self):
        print(
"""Preface: The 'guided' command is a wrapper for commands described under this
entry. As such, it may be simpler to use that command instead.

    There are three main steps to start scraping for attendance links: Setting
up required information, selecting what to search, and lastly, starting the
attendance search itself.

First step:     Setting up required information.
————————————————————————————————————————————————————————————————————————————————
Commands:       login
Syntax:         login [student_id]

    The script requires certain information before it could start. For instance,
as typical sought-after attendance links are for registered subjects, the script
is able to programmatically obtain required information by simply logging in to
MMLS.

Second step:    Displaying and selecting what to search.
————————————————————————————————————————————————————————————————————————————————
Commands:       print, select, deselect, toggle, and autoselect.
Syntax:         print
                select|deselect|toggle <i.e. '1a 2abc 3' and 'all'>
                autoselect

    After the script has logged in and parsed the required information, a list
of subjects and its classes could be displayed by using the 'print' command.
There will be selection boxes accompanying the class entries which signifies
whether the class will be searched for its attendance links. Altering class
selections can be done using the 'select', 'deselect', and 'toggle' command. On
the other hand, the 'autoselect' command is available which can automatically
select registered classes.

Third step:     Starting attendance link scraping.
————————————————————————————————————————————————————————————————————————————————
Commands:       search date and search timetable.
Syntax:         search date <start_date> <end_date>
                search date <date>
                search date ...leave empty to search for today.
                search timetable <start_timetable_id> <end_timetable_id>

    The way this script works is by iterating through timetable IDs for selected
class IDs then formats them into an attendance link if the timetable ID belongs
to one of the selected classes. A date search can be conducted by using the
'search date' command with date entered in the format of yyyy-mm-dd. The command
searches for the first occurence and last occurence of the timetable IDs which
falls into the specified date range and uses those as the attendance timetable
ID search range. Alternatively, one could skip the date search by manually
providing the timetable ID range to search using the 'search timetable' command.
Found attendance links are displayed as it is searched.\n""")

if os.name == 'nt': #aiohttp is noisy/buggy with ProactorEventLoop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if __name__ == '__main__':
    subjects_db = SubjectsDB()
    prompt = Prompt()
    prompt.prompt = '> '
    prompt.intro = ("                                           \n"
                    " _____ _____ __    _____                   \n"
                    "|     |     |  |  |   __|   .\\\\To start:   \n"
                    "| | | | | | |  |__|__   |    > guided      \n"
                    "|_|_|_|_|_|_|_____|_____|_                 \n"
                    "|  _  | |_| |_ ___ ___ _| |___ ___ ___ ___ \n"
                    "|     |  _|  _| -_|   | . | .'|   |  _| -_|\n"
                    "|__|__|_| |_| |___|_|_|___|__,|_|_|___|___|\n"
                    "|   __|___ ___ ___ ___ ___ ___             \n"
                    "|__   |  _|  _| .'| . | -_|  _|            \n"
                    "|_____|___|_| |__,|  _|___|_|              \n"
                    "                  |_|          ...pls fix  \n"
                    "                                           \n"
                    "Enter 'help' or '?' to list commands.      \n")
    prompt.ruler = '—'
    try:
        prompt.cmdloop()
    except KeyboardInterrupt:
        print()
        prompt.do_exit('')
