from datetime import date, datetime, timedelta
import asyncio
import cmd
import getpass
import mmlsattendance
import os
import re

PRINT_ATTENDANCE_LIST = False

def print_subjects(SubjectDB_obj):
    num_pad = len(str(len(SubjectDB_obj.subjects)))
    space = ' '*num_pad + ' '*2
    cat_space = space + ' '*3
    for subject_no, subject in enumerate(SubjectDB_obj.subjects, 1):
        subj_no = str(subject_no).rjust(num_pad)
        print(f"{subj_no}. {subject.code} - {subject.name}")        # 1. ECE2056 - DATA COMM AND NEWORK
        print(f"{space}> Subject ID: {subject.id}")                 #    > Subject ID: 232
        print(f"{space}> Coordinator ID: {subject.coordinator_id}") #    > Coordinator ID: 1577623541
        print(f"{cat_space}Sel Class Class ID")                     #       Sel Class Class ID
        for char_id, kelas in enumerate(subject.classes, ord('a')): #    a. [X]  EC01    45132
            X = 'X' if kelas.selected else ' '                      #    b. [ ]  ECA1    45172
            print(f"{space}{chr(char_id)}. [{X}]{kelas.code:>6}{kelas.id:>9}")
        if subject_no != len(SubjectDB_obj.subjects):
            print()

async def printer(queue):
    while True:
        f = await queue.get()
        print()
        print(f"[{f.class_date} {f.start_time}-{f.end_time}] {f.subject_code} - {f.subject_name} ({f.class_code})")
        print(f.attendance_url)
        if PRINT_ATTENDANCE_LIST:
            if f.attendance_list_url:
                print(f.attendance_list_url)
        queue.task_done()

def change_selection(args, op):
    """Parses selection command arguments, creates a dict of sets -- where
    key is subject index and value is a set of class index -- and iterates
    through each item and its set elements which through it does select,
    deselect or toggle operation to classes at their index."""
    args_list = args.lower().split() # E.g. ['1ab', '2ac', '3', '4a']
    if args_list and args_list[0] == 'all':
        for kelas in subject_db.classes:
            kelas.selected = op if op is not None else not kelas.selected
    else:
        op_dict = {} #op_dict = {subject_index: {class_index, ...}, ...}
        for arg in args_list:
            re_obj = re.match('[0-9]+', arg[0])
            if re_obj is None:
                continue
            sub_idx = int(re_obj[0])-1
            if sub_idx < 0:
                continue
            re_obj = re.search('[a-z]+', arg)
            letters = re_obj[0] if re_obj is not None else ''
            class_choices = [ord(char)-ord('a') for char in letters]
            op_dict[sub_idx] = set(class_choices)
        if not op_dict:
            return False
        subjects = subject_db.subjects
        for sub_idx, class_set in op_dict.items():
            if not class_set:
                try:
                    for kelas in subjects[sub_idx].classes:
                        kelas.selected = op if op is not None else not kelas.selected
                except IndexError:
                    pass
            else:
                for cls_idx in class_set:
                    try:
                        subjects[sub_idx].classes[cls_idx].selected = op if op is not None else not subjects[sub_idx].classes[cls_idx].selected
                    except IndexError:
                        pass
    return True

class Prompt(cmd.Cmd):
    nohelp = "No help on '%s'.\n"
    user_id = None

    def do_login(self, args):
        ("\n"
        "Log in to MMLS and MMU Mobile and load subjects and classes.\n"
        "————————————————————————————————————————————————————————————\n"
        "Syntax:   login [student_id]                                \n")
        user_id = args.split()[0] if args else input('Student ID: ')
        password = getpass.getpass()
        if asyncio.run(mmlsattendance.load_online(subject_db, user_id, password)):
            print('Success.\n')
            self.user_id = user_id
        else:
            print('Wrong student ID or password.\n')

    def do_print(self, args):
        ("\nDisplay stored subjects, classes and selection.\n")
        print_subjects(subject_db)
        print()

    def do_autoselect(self, args):
        ("\nAuto-select classes that the student has registered for.\n")
        if self.user_id is not None:
            asyncio.run(mmlsattendance.autoselect_classes(subject_db, self.user_id))
            print_subjects(subject_db)
            print()
        else:
            print('Please log in to use this command.\n')

    def do_select(self, args):
        ("\n"
        "Add selection to classes.      \n"
        "———————————————————————————————\n"
        "Examples: select 1a 2c 3 4abc 5\n"
        "          select all           \n")
        if not change_selection(args, True):
            print("Invalid command. Enter 'help search' for command help.\n")
            return
        print_subjects(subject_db)
        print()

    def do_deselect(self, args):
        ("\n"
        "Remove selection in classes.     \n"
        "—————————————————————————————————\n"
        "Examples: deselect 1a 2c 3 4abc 5\n"
        "          deselect all           \n")
        if not change_selection(args, False):
            print("Invalid command. Enter 'help search' for command help.\n")
            return
        print_subjects(subject_db)
        print()

    def do_toggle(self, args):
        ("\n"
        "Toggle selection of classes.   \n"
        "———————————————————————————————\n"
        "Examples: toggle 1a 2c 3 4abc 5\n"
        "          toggle all           \n")
        if not change_selection(args, None):
            print("Invalid command. Enter 'help search' for command help.\n")
            return
        print_subjects(subject_db)
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
        if not subject_db.selected_classes:
            print('No classes selected for searching.\n')
            return
        elif not cmd or not (cmd == 'date' or cmd == 'timetable'):
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
            if len(args_list) == 2:
                print(f"Searching classes from {start_date.isoformat()} to {end_date.isoformat()}.")
            else:
                print(f"Searching classes in {start_date.isoformat()}.")
            async def scrape_and_print(start_date, end_date, subject_db):
                queue = asyncio.Queue()
                printer_task = asyncio.create_task(printer(queue))
                await mmlsattendance.scrape_date(subject_db, start_date, start_date, queue = queue)
                await queue.join()
                printer_task.cancel()
                await asyncio.wait([printer_task])
            asyncio.run(scrape_and_print(start_date, end_date, subject_db))
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
            async def scrape_and_print(start_timetable_id, end_timetable_id, subject_db):
                queue = asyncio.Queue()
                printer_task = asyncio.create_task(printer(queue))
                await mmlsattendance.scrape(subject_db, start_timetable_id, end_timetable_id, queue = queue)
                await queue.join()
                printer_task.cancel()
                await asyncio.wait([printer_task])
            asyncio.run(scrape_and_print(start_timetable_id, end_timetable_id, subject_db))
        print()

    def do_exit(self, args):
        ("\nTerminate this script.\n")
        print('Exiting.')
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
        if self.user_id is None:
            self.do_login('')
        if self.user_id is None:
            return
        self.do_print('')
        if ask_yes_no('Auto-select your registered classes?'):
            print()
            self.do_autoselect('')
        if ask_yes_no('Edit class selection?'):
            self.do_print('')
            while True:
                try:
                    sub_idx = int(input('Select which subject: '))-1
                    class_choices = input("Toggle which classes?: ").replace(',', '').split(' ')
                    class_indexes = [ord(char)-ord('a') for char in class_choices]
                    for index in class_indexes:
                        subject_db.subjects[sub_idx].classes[index].selected = not subject_db.subjects[sub_idx].classes[index].selected
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
            start_timetable_id = input('Define beginning of timetable_id range: ')
            end_timetable_id = input('Define end of timetable_id range: ')
            self.do_search(f'timetable {start_timetable_id} {end_timetable_id}')

    def default(self, line):
        print(f"Command not found: '{line}'. Enter 'help' to list commands.\n")

    def emptyline(self):
        print('Hello?\n')

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
    subject_db = mmlsattendance.SubjectDB()
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
