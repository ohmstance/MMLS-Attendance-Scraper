from urllib import request, error, parse
from lxml import etree
from datetime import date, datetime, timedelta
import cmd
import concurrent.futures
import time
import getpass
import json
import os

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
WORKERS = min(32, os.cpu_count()*5)
NETWORK_TIMEOUT = 15
NETWORK_RETRIES = 3
NETWORK_RETRY_BACKOFF = 2
MAX_TIMETABLE_ID = 99999
MIN_TIMETABLE_ID = 1

def request_url(url, method='GET', *, data={}, headers={}):
    if method == 'POST':
        data = parse.urlencode(data).encode('utf-8')
        req = request.Request(url, data=data, headers=headers, method='POST')
    else: #defaults to GET
        data = parse.urlencode(data)
        req = request.Request(f"{url}?{data}", data=None, headers=headers, method='GET')
    for _ in range(NETWORK_RETRIES):
        try:
            return request.urlopen(req, timeout=NETWORK_TIMEOUT)
        except TimeoutError:
            time.sleep(NETWORK_RETRY_BACKOFF)

def get_attendance_etree(timetable_id): #Accepts timetable_id. Parses attendance HTTP response of input timetable_id. Returns ElementTree object, but None type if failed.
    try:
        html = request_url(f"{BASE_ATTENDANCE_URL}:0:0:{timetable_id}", 'GET')
    except error.HTTPError as err:
        if err.code == 500:
            return None
    return etree.parse(html, etree.HTMLParser())

def date_to_timetable_id(date, option, upperbound = MAX_TIMETABLE_ID, lowerbound = MIN_TIMETABLE_ID):
    class_date_xpath =  "//input[@name='class_date']/@value"
    while(True): #Option: 1 for first occurence, -1 for last occurence; Binary search algorithm; Returns None if no class on that date.
        current_timetable_id = (upperbound+lowerbound)//2
        html_etree = get_attendance_etree(current_timetable_id)
        if html_etree is None:
            upperbound = current_timetable_id-1
            continue
        current_date = date.fromisoformat(html_etree.xpath(class_date_xpath)[0])
        if (date - current_date).days > 0:
            lowerbound = current_timetable_id+1
        elif (date - current_date).days < 0:
            upperbound = current_timetable_id-1
        else:
            look_ahead_etree = get_attendance_etree(current_timetable_id-option)
            if (look_ahead_etree is None or
                date.fromisoformat(look_ahead_etree.xpath(class_date_xpath)[0]) != current_date):
                return current_timetable_id
            if option == 1:
                upperbound = current_timetable_id-1
            elif option == -1:
                lowerbound = current_timetable_id+1
        if upperbound < lowerbound:
            return None

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

    def _request_url(self, url, method, *, data={}, headers={}):
        if method == 'POST':
            data = parse.urlencode(data).encode('utf-8')
            req = request.Request(url, data=data, headers=headers, method='POST')
        else: #defaults to GET
            data = parse.urlencode(data)
            req = request.Request(f"{url}?{data}", data=None, headers=headers, method='GET')
        for _ in range(NETWORK_RETRIES):
            try:
                return request.urlopen(req, timeout=NETWORK_TIMEOUT)
            except TimeoutError:
                time.sleep(NETWORK_RETRY_BACKOFF)
                continue

    def _parse_classes(self, subject): #Accepts subject dict in SubjectListDB and cookie for MMLS. Returns a list of class dicts.
        subject_id, coordinator_id = subject['subject_id'], subject['coordinator_id']
        subject_student_list_url = f"{BASE_STUDENT_LIST_URL}:{subject_id}:{coordinator_id}:0"
        response = self._request_url(subject_student_list_url, 'GET', headers={'Cookie' : self.cookie})
        tree = etree.parse(response, etree.HTMLParser())
        class_names = tree.xpath("//select[@id='select_class']/*[not(self::option[@value='0'])]/text()")
        class_ids = tree.xpath("//select[@id='select_class']/*[not(self::option[@value='0'])]/@value")
        return [{'class_name' : name, 'class_id' : id, 'selected' : False} for name, id in zip(class_names, class_ids)]

    def _is_user_in_class(self, user_id, class_id): #Returns True if user in class, False otherwise.
        cookie = request.urlopen('https://mmls.mmu.edu.my/attendance:0:0:1').info()['Set-Cookie']
        data = {'stud_id' : user_id, 'stud_pswrd' : '0', 'class_id' : class_id}
        headers = {'Cookie' : cookie, 'Referer' : 'https://mmls.mmu.edu.my/attendance:0:0:1'}
        response = self._request_url(MMLS_ATTENDANCE_LOGIN_URL, 'POST', data=data, headers=headers)
        tree = etree.parse(response, etree.HTMLParser())
        not_in_class = tree.xpath("//div[@class='alert alert-danger']/text()='You are not register to this class.'")
        return False if not_in_class else True

    def init_mmls(self):
        response = request.urlopen(MMLS_URL)
        tree = etree.parse(response, etree.HTMLParser())
        self.cookie = response.info()['Set-Cookie']
        self.token = tree.xpath("//input[@name='_token']/@value")[0]

    def login(self, user_id, password):
        if not (self.token or self.cookie):
            self.init_mmls()
        data = {'stud_id' : user_id, 'stud_pswrd' : password, '_token' : self.token}
        headers = {'Cookie' : self.cookie}
        try:
            response = self._request_url(MMLS_LOGIN_URL, 'POST', data=data, headers=headers)
        except error.HTTPError as err:
            if err.code == 500:
                return False
        self.user_id = user_id
        self.load_subjects(response)
        return True

    def login_mobile(self, user_id, password):
        data = {'username' : user_id, 'password' : password}
        try:
            response = self._request_url(MOBILE_LOGIN_URL, 'POST', data=data)
        except error.HTTPError as err:
            if err.code == 422:
                return False
        self.mobile_token = json.loads(response.read())['token']
        self.first_trimester_date()
        return True

    def logout(self):
        if self.cookie is not None:
            headers = {'Cookie' : self.cookie}
            self._request_url(MMLS_LOGOUT_URL, 'GET', headers=headers)

    def logout_mobile(self):
        if self.mobile_token is not None:
            data = {'token' : self.mobile_token}
            self._request_url(MOBILE_LOGOUT_URL, 'POST', data=data)

    def load_subjects(self, response = None):
        if not response:
            headers = {'Cookie' : self.cookie}
            response = self._request_url(f"{MMLS_URL}home", 'GET', headers=headers)
        tree = etree.parse(response, etree.HTMLParser())
        names = tree.xpath("//div[@class='list-group ' and @style='margin-top:-15px']/span/a[1]/text()")
        names = [name.split(' - ') for name in names] # ECE2056 - DATA COMM AND NEWORK
        links = tree.xpath("//div[@class='list-group ' and @style='margin-top:-15px']/span/a[1]/@href")
        links = [link[24:].split(':') for link in links] # https://mmls.mmu.edu.my/232:1592795134
        names_and_links = [[data for nested_list in zipped for data in nested_list] for zipped in zip(names, links)]
        temp_subjects_list = [{
            'subject_code': subject_code, #Eg. ECE2056
            'subject_name': subject_name, #Eg. DATA COMM AND NEWORK
            'subject_id': subject_id, #Eg. 332
            'coordinator_id': coordinator_id, #Eg. 1585369691
            'classes' : [] #List of classes in dict, with its class code and select attribute.
        } for subject_code, subject_name, subject_id, coordinator_id in names_and_links]
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [executor.submit(self._parse_classes, subject) for subject in temp_subjects_list]
            for subject_index, classes in enumerate(futures):
                temp_subjects_list[subject_index]['classes'] = classes.result()
        for subject_index, subject in enumerate(self.subjects_db):
            for temp_subject_index, temp_subject in enumerate(temp_subjects_list):
                if temp_subject['subject_id'] == subject['subject_id']:
                    self.subjects_db[subject_index] = temp_subject
                    del temp_subjects_list[temp_subject_index]
        self.subjects_db.extend(temp_subjects_list)
        self.update_hash()

    def autoselect_classes(self):
        if self.registered_classes:
            for class_id in self.registered_classes:
                self.get_class(class_id).update({'selected': True})
            return True
        elif self.user_id is not None:
            with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
                futures = [[class_id, executor.submit(self._is_user_in_class, self.user_id, class_id)] for class_id in self.class_id_to_index_dict.keys()]
                for class_id, is_in_class in futures:
                    if is_in_class.result():
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

    def first_trimester_date(self):
        if self.trimester_start_date:
            return self.trimester_start_date
        data = {'token' : self.mobile_token}
        response = self._request_url(MOBILE_SUBJECT_LIST_URL, 'GET', data=data)
        JSON = json.loads(response.read())
        self.trimester_start_date = date.fromisoformat(JSON[0]['sem_start_date']) #Get sem_start_date of the first subject in MMU Mobile subject list
        return self.trimester_start_date

class Prompt(cmd.Cmd):
    nohelp = "No help on '%s'.\n"

    def do_login(self, args):
        ("\n"
        "Log in to MMLS and MMU Mobile and load subjects and classes.\n"
        "————————————————————————————————————————————————————————————\n"
        "Syntax:   login [student_id]                                \n")
        user_id = args.split()[0] if args else input('Student ID: ')
        password = getpass.getpass()
        if subjects_db.login(user_id, password):
            subjects_db.login_mobile(user_id, password)
            print('Success.\n')
        else:
            print('Wrong student ID or password.\n')

    def do_print(self, args):
        ("\nDisplay stored subjects, classes and selection.\n")
        print_subjects(subjects_db.subjects_db)
        print()

    def do_autoselect(self, args):
        ("\nAuto-select classes that the student has registered for.\n")
        if subjects_db.autoselect_classes():
            print_subjects(subjects_db.subjects_db)
            print()
        else:
            print('Please log in to use this command.\n')

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
            print('No classes selected for searching.\n')
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
            if not cmd or not (cmd == 'date' or cmd == 'timetable'):
                print("Invalid command. Enter 'help search' for command help.\n")
                return
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
                trimester_start_date = subjects_db.first_trimester_date()
                if 0 <= (start_date - trimester_start_date).days <= 2 or 0 <= (start_date - trimester_start_date).days <= 2:
                    print("WARNING: Date search is extremely unreliable in the first three trimester days.\n"
                          "         Expect missing or no attendance links. Use timetable search instead!")
                start_timetable_id = end_timetable_id = None
                while True:
                    if start_timetable_id is None:
                        start_timetable_id = executor.submit(date_to_timetable_id, start_date, 1)
                    if end_timetable_id is None:
                        end_timetable_id = executor.submit(date_to_timetable_id, end_date, -1)
                    start_timetable_id = start_timetable_id if isinstance(start_timetable_id, int) else start_timetable_id.result()
                    end_timetable_id = end_timetable_id if isinstance(end_timetable_id, int) else end_timetable_id.result()
                    if not start_timetable_id:
                        start_date += timedelta(days=1)
                    if not end_timetable_id:
                        end_date -= timedelta(days=1)
                    if start_date > end_date:
                        print('No classes found within date range.\n')
                        return
                    elif start_timetable_id and end_timetable_id:
                        print(f"Searching classes from {start_timetable_id} ({start_date.isoformat()}) to {end_timetable_id} ({end_date.isoformat()}).")
                        timetable_id_range = end_timetable_id-start_timetable_id+1
                        futures = [executor.submit(get_attendance_etree, start_timetable_id+x) for x in range(timetable_id_range)]
                        break
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
                timetable_id_range = end_timetable_id-start_timetable_id+1
                futures = [executor.submit(get_attendance_etree, start_timetable_id+x) for x in range(timetable_id_range)]
            # =============== Parsing and printing ===============
            found_attendance_link = False
            while futures:
                html_etree = futures.pop(0).result()
                if not html_etree:
                    for future in futures:
                        concurrent.futures.Future.cancel(future)
                    del futures
                    break
                parsed_class_id = html_etree.xpath("//input[@name='class_id']/@value")[0]
                if (subjects_db.is_class_in_database(parsed_class_id) and
                    subjects_db.is_class_selected(parsed_class_id)):
                    print()
                    print_links(html_etree)
                    found_attendance_link = True
            if not found_attendance_link:
                print('No links from selected classes found.')
        print()

    def do_exit(self, args):
        ("\nLog out both MMLS and MMU Mobile then terminate this script.\n")
        print('Exiting.')
        subjects_db.logout()
        subjects_db.logout_mobile()
        exit()

    def change_selection(self, args, op):
        """Parses selection command arguments, creates a dict of sets -- where
        key is subject index and value is a set of class index -- and iterates
        through each item and its set elements which through it does select,
        deselect or toggle operation to classes at their index."""
        if args and args.lower().split()[0] == 'all':
            subjects_db.selector(op, None, None)
        else:
            args_list = args.lower().split() # E.g. ['1ab', '2ac', '3', '4a']
            op_dict = {} #op_dict = {subject_index: {class_index, ...}, ...}
            match_int = {str(i) for i in range(10)}
            for arg in args_list:
                # ===== Get subject index =====
                subject_no = ''
                for char in arg:
                    if char not in match_int:
                        break
                    subject_no += char
                if len(subject_no) == 0:
                    continue
                # ===== Get class indexes =====
                class_choices = [ord(char)-ord('a') for char in arg[len(subject_no):]]
                for index, char_int in enumerate(class_choices):
                    if char_int < 0 or char_int > ord('z')-ord('a'):
                        class_choices = class_choices[:index+1]
                        break
                op_dict[int(subject_no)-1] = set(class_choices)
            if not op_dict:
                return False
            for subject_index, class_set in op_dict.items():
                if not class_set:
                    subjects_db.selector(op, subject_index, None)
                else:
                    for class_index in class_set:
                        subjects_db.selector(op, subject_index, class_index)
        return True

    # def parse_flag_args(self, args): #TODO: Need better implementation for no-argument flags
    #     args_list = args.lower().split()
    #     args_dict = {}
    #     prev_arg = ''
    #     for arg in args_list:
    #         if prev_arg[:1] == '-' and arg[:1] != '-':
    #             args_dict[prev_arg[1:]] = arg
    #         prev_arg = arg
    #     return args_dict

    def default(self, line):
        print(f"Command not found: '{line}'. Enter 'help' to list commands.\n")

    def help_help(self):
        print("\n"
        "List available commands or provide command help.\n"
        "————————————————————————————————————————————————\n"
        "Syntax:   help [command]                        \n")

    def help_syntax_format(self):
        print("\n"
        "Arguments in...                      \n"
        "    1. Angle brackets is <required>  \n"
        "    2. Square brackets is [optional] \n")

if __name__ == '__main__':
    subjects_db = SubjectsDB()
    prompt = Prompt()
    prompt.prompt = '> '
    prompt.intro = ("                                           \n"
                    " _____ _____ __    _____   .\\\\Steps:       \n"
                    "|     |     |  |  |   __|   > login        \n"
                    "| | | | | | |  |__|__   |   > autoselect   \n"
                    "|_|_|_|_|_|_|_____|_____|_  > search date  \n"
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
