from __future__ import annotations
from datetime import date, datetime, timedelta
from inspect import cleandoc
from typing import Union
import asyncio
import cmd
import getpass
import mmlsattendance
import os
import re
# import logging

# logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
PRINT_ATTENDANCE_LIST = False


def print_subjects(i_courses: mmlsattendance.Courses) -> None:
    num_pad = len(str(len(i_courses.subjects)))
    space = ' ' * num_pad + ' ' * 2
    cat_space = space + ' ' * 3
    for subject_no, subject in enumerate(i_courses.subjects, 1):
        subj_no = str(subject_no).rjust(num_pad)
        print(f"{subj_no}. {subject.code} - {subject.name}")
        print(f"{space}> Subject ID: {subject.id}")
        print(f"{space}> Coordinator ID: {subject.coordinator_id}")
        print(f"{cat_space}Sel Class Class ID")
        for char_id, kelas in enumerate(subject.classes, ord("a")):
            x = "X" if kelas.selected else " "
            print(f"{space}{chr(char_id)}. [{x}]{kelas.code:>6}{kelas.id:>9}")
        if subject_no != len(i_courses.subjects):
            print()


def print_attendance(d_form: mmlsattendance.DetailedAttendanceForm) -> None:
    print()
    print(
        f"[{d_form.class_date} {d_form.start_time[:-3]}-{d_form.end_time[:-3]}] "
        f"{d_form.subject_code} - {d_form.subject_name} ({d_form.class_code})"
    )
    print(d_form.attendance_url)
    if PRINT_ATTENDANCE_LIST:
        if d_form.attendance_list_url:
            print(d_form.attendance_list_url)


def change_selection(args: str, op: Union[bool, None]) -> bool:
    """
    Parses selection command arguments, creates a dict of sets -- where key is subject index and value is a set of class
    index -- and iterates through each item and its set elements which through it does select, deselect or toggle
    operation to classes at their index.
    """
    args_list = args.lower().split()  # E.g. ['1ab', '2ac', '3', '4a']
    if args_list and args_list[0] == 'all':
        for kelas in courses.classes:
            kelas.selected = op if op is not None else not kelas.selected
    else:
        op_dict = {}  # op_dict = {subject_index: {class_index, ...}, ...}
        for arg in args_list:
            re_obj = re.match("[0-9]+", arg[0])
            if re_obj is None:
                continue
            sub_idx = int(re_obj[0]) - 1
            if sub_idx < 0:
                continue
            re_obj = re.search("[a-z]+", arg)
            letters = re_obj[0] if re_obj is not None else ''
            class_choices = [ord(char) - ord('a') for char in letters]
            op_dict[sub_idx] = set(class_choices)
        if not op_dict:
            return False
        subjects = courses.subjects
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
                        subjects[sub_idx].classes[cls_idx].selected = op if op is not None else not \
                            subjects[sub_idx].classes[cls_idx].selected
                    except IndexError:
                        pass
    return True


class Prompt(cmd.Cmd):
    nohelp = "No help on '%s'."
    user_id: Union[str, None] = None

    def do_login(self, args: str):
        """
        Description:
            Log in to MMLS and MMU Mobile and load subjects and classes.
        Usage:
            login [student_id]
        """

        user_id = args.split()[0] if args else input("Student ID: ")
        password = getpass.getpass()
        if loop.run_until_complete(courses.load_online(user_id, password)):
            print("Success.")
            self.user_id = user_id
        else:
            print("Invalid student ID or password.")

    def do_save(self, args: str):
        """
        Description:
            Save current subjects and classes.
        Usage:
            save
        """
        if self.user_id is None:
            print("Log in to use this command.")
            return
        if os.path.exists("courses.json"):
            os.remove("courses.json")
        with open("courses.json", "w") as f_file:
            f_file.write(self.user_id + "\n")
            f_file.write(courses.json())
        print("Courses saved.")

    def do_load(self, args: str):
        """
        Description:
            Load saved subjects and classes.
        Usage:
            load
        """
        try:
            with open("courses.json", "r+") as f_file:
                self.user_id = f_file.readline()
                courses.load_json(f_file.read())
            print("Courses loaded.")
        except FileNotFoundError:
            print("No courses were saved.")

    def do_print(self, args: str):
        """
        Description:
            Display stored subjects, classes and selection.
        Usage:
            print
        """
        print_subjects(courses)

    def do_autoselect(self, args: str):
        """
        Description:
            Auto-select classes that the student has registered for.
        Usage:
            autoselect
        """
        if self.user_id is not None:
            loop.run_until_complete(courses.autoselect_classes(self.user_id))
            print_subjects(courses)
        else:
            print("Log in to use this command.")

    def do_select(self, args: str):
        """
        Description:
            Add selection of classes.
        Usage:
            select [number]+[character]* ... ...
            select all
        """
        if not change_selection(args, True):
            print("Invalid command. Enter 'help select' for command help.")
            return
        print_subjects(courses)

    def do_deselect(self, args: str):
        """
        Description:
            Remove selection of classes.
        Usage:
            deselect [number]+[character]* ... ...
            deselect all
        """
        if not change_selection(args, False):
            print("Invalid command. Enter 'help deselect' for command help.")
            return
        print_subjects(courses)

    def do_toggle(self, args: str):
        """
        Description:
            Toggle selection of classes.
        Usage:
            toggle [number]+[character]* ... ...
            toggle all
        """
        if not change_selection(args, None):
            print("Invalid command. Enter 'help toggle' for command help.")
            return
        print_subjects(courses)

    def do_search(self, args: str):
        """
        Description:
            Search for attendance links in a specified range.
        Usage:
            search {date|fastdate} <start_date> <end_date>
            search {date|fastdate} [date]
            ...if date is not provided, uses current date.
            search timetable <start_id> <end_id>
        Addendum:
            Dates need to be entered in ISO format, YYYY-MM-DD.
            Using 'fastdate' is faster than 'date', but might miss some attendance URLs.
        """
        i_cmd = ''.join(args.split()[:1])
        args = ' '.join(args.split()[1:])
        if not courses.selected_classes:
            print('No classes selected for searching.')
            return
        elif not i_cmd or not (i_cmd == 'fastdate' or i_cmd == 'date' or i_cmd == 'timetable'):
            print("Invalid command. Enter 'help search' for command help.")
            return
        # =============== search date <start_date> <end_date> ===============
        elif i_cmd == 'date' or i_cmd == 'fastdate':
            args_list = args.split()
            try:
                if len(args_list) > 2:
                    print("Too many arguments. Enter 'help search' for command help.")
                    return
                elif len(args_list) == 1:
                    start_date = end_date = date.fromisoformat(args_list[0])
                elif len(args_list) == 0:
                    start_date = end_date = (datetime.utcnow() + timedelta(hours=8)).date()
                else:
                    start_date = date.fromisoformat(args_list[0])
                    end_date = date.fromisoformat(args_list[1])
            except ValueError as err:
                print(f"{err}.")
                return
            if len(args_list) == 2:
                print(f"Searching classes from {start_date.isoformat()} to {end_date.isoformat()}.")
            else:
                print(f"Searching classes in {start_date.isoformat()}.")
            fast = True if i_cmd == 'fastdate' else False

            async def scrape_and_print():
                async for result in scraper.scrape_date(courses, start_date, end_date, fast=fast):
                    print_attendance(result)

            loop.run_until_complete(scrape_and_print())

        # =============== search timetable <start_id> <end_id> ===============
        elif i_cmd == 'timetable':
            args_list = args.split()
            if not len(args_list) == 2:
                print("Expected two arguments. Enter 'help search' for command help.")
                return
            try:
                start_timetable_id = int(args_list[0])
                end_timetable_id = int(args_list[1])
            except ValueError as err:
                print(f"Value error: {err}. Enter 'help search' for command help.")
                return
            print(f"Searching classes from {start_timetable_id} to {end_timetable_id}.")

            async def scrape_and_print():
                async for result in scraper.scrape(courses, start_timetable_id, end_timetable_id):
                    print_attendance(result)

            loop.run_until_complete(scrape_and_print())

    def do_cache(self, args: str):
        """
        Description:
            Cache attendances in a specified timetable ID range.
        Usage:
            cache <start_id> <end_id>
            cache all
        """
        args_list = args.split()
        try:
            if len(args_list) == 1:
                if args_list[0] == 'all':
                    start_ttid = end_ttid = None
                else:
                    print(f"Invalid argument. Enter 'help cache' for command help.")
                    return
            elif len(args_list) == 2:
                start_ttid = int(args_list[0])
                end_ttid = int(args_list[1])
            elif len(args_list) == 0:
                print(f"Missing arguments. Enter 'help cache' for command help.")
                return
            else:
                print(f"Too many arguments. Enter 'help cache' for command help.")
                return
        except ValueError:
            print(f"Value error. Enter 'help cache' for command help.")
            return
        num_cached = len(scraper.attendance_cache)
        print('This could take a while. Working...')
        scraper.attendance_cache = loop.run_until_complete(
            mmlsattendance.update_cache(scraper.attendance_cache,
                                        update_cached=True, start_timetable_id=start_ttid, end_timetable_id=end_ttid)
        )
        new_num_cached = len(scraper.attendance_cache)
        print(f"Cached: {new_num_cached} (+{new_num_cached - num_cached})")
        print('Done!')

    def do_cached(self, args: str):
        """
        Description:
            Print the number of cached attendances.
        Usage:
            cached
        """
        print(f'Total cached: {len(scraper.attendance_cache)}')

    def do_exit(self, args: str):
        """
        Description:
            Terminate this script.
        Usage:
            exit
        """
        print('Exiting.')
        exit()

    def do_guided(self, args: str):
        """
        Description:
            Start a guided setup for typical attendance scraping.
        Usage:
            guided
        """

        def ask_yes_no(question):
            while True:
                decision = input(f"{question} (y/n): ")
                if decision.lower() == 'y':
                    return True
                if decision.lower() == 'n':
                    return False
                print("Invalid input.")

        print(cleandoc(
            """
            How do you want to scrape attendance links?:
            1. Retrieve classes via MMLS login and search by fastdate.*
            2. Retrieve classes via MMLS login and search by date.
            2. Retrieve classes via MMLS login and search by range of timetable_id.

            * Unreliable in the first three trimester days and in some cases.
              If no links were caught use the second option instead.
            """
        ))
        while True:
            try:
                what_to_do = int(input("\nChoice: "))
                if not 0 < what_to_do < 4:
                    raise ValueError
                break
            except ValueError:
                print("Invalid input.")
        if self.user_id is None:
            self.do_login('')
        if self.user_id is None:
            return
        self.do_print("")
        if ask_yes_no("Auto-select your registered classes?"):
            self.do_autoselect('')
        if ask_yes_no("Edit class selection?"):
            self.do_print('')
            while True:
                try:
                    sub_idx = int(input("Select which subject: ")) - 1
                    class_choices = input("Toggle which classes?: ").replace(',', '').split(' ')
                    class_indexes = [ord(char) - ord('a') for char in class_choices]
                    for index in class_indexes:
                        courses.subjects[sub_idx].classes[index].selected = \
                            not courses.subjects[sub_idx].classes[index].selected
                    self.do_print('')
                except (ValueError, TypeError):
                    print("Invalid input.")
                if not ask_yes_no("Continue editing?"):
                    break
        if what_to_do == 1:
            start_date = input("Search from what date? YYYY-MM-DD: ")
            end_date = input("Until what date? YYYY-MM-DD: ")
            self.do_search(f"fastdate {start_date} {end_date}")
        elif what_to_do == 2:
            start_date = input("Search from what date? YYYY-MM-DD: ")
            end_date = input("Until what date? YYYY-MM-DD: ")
            self.do_search(f"date {start_date} {end_date}")
        elif what_to_do == 3:
            start_timetable_id = input("Define beginning of timetable_id range: ")
            end_timetable_id = input("Define end of timetable_id range: ")
            self.do_search(f"timetable {start_timetable_id} {end_timetable_id}")

    def default(self, line: str):
        print(f"Command not found: '{line}'. Enter 'help' to list commands.")

    def emptyline(self):
        print("Hello?")

    def help_manual(self):
        print(cleandoc(
            """
            Preface: The 'guided' command is a wrapper for commands described under this
            entry. As such, it may be simpler to use that command instead.
            
                There are three main steps to start scraping for attendance links: Setting
            up required information, selecting what to search, and lastly, starting the
            attendance search itself.
            
            First step:     Setting up required information.
            ————————————————————————————————————————————————————————————————————————————————
            Commands:       login
            Usage:          login [student_id]
            
                The script requires certain information before it could start. The script
            is able to programmatically obtain required information by simply logging in to
            MMLS.
            
            Second step:    Displaying and selecting what to search.
            ————————————————————————————————————————————————————————————————————————————————
            Commands:       print, select, deselect, toggle, and autoselect.
            Usage:          print
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
            Commands:       search fastdate, search date and search timetable.
            Usage:          search {date|fastdate} <start_date> <end_date>
                            search {date|fastdate} [date]
                            ...if date is not provided, uses current date.
                            search timetable <start_timetable_id> <end_timetable_id>
            
                The way this script works is by iterating through timetable IDs for selected
            class IDs then formats them into an attendance link if the timetable ID belongs
            to one of the selected classes. A date search can be conducted by using the
            'search date' and 'search fastdate' command with date entered in the format of 
            yyyy-mm-dd. 'fastdate' searches for the first occurence and last occurence of 
            the timetable IDs which falls within the specified date range and uses those as
            the timetable ID search range. As it is using binary search for the exact
            timetable ID that is at the beginning and the end of date range, it is 
            unreliable when it encounters timetable IDs that are not sorted by date. To 
            mitigate this, use 'date' subcommand instead which looks for a timetable ID 1-2
            months prior to the specified date. Although much slower, this makes it, with a 
            high degree of confidence, all timetable IDs that falls within the specified 
            date range are parsed and checked. Alternatively, one could skip the date search 
            by manually providing the timetable ID range to search using the 'search
            timetable' command. Found attendance links are displayed as it is searched.
            """
        ))

    def do_help(self, arg: str):
        """
        Description:
            List available commands or provide command help.
        Usage:
            help [command]
        """
        if arg:
            # XXX check arg syntax
            try:
                func = getattr(self, 'help_' + arg)
            except AttributeError:
                try:
                    # EDIT: Use inspect.cleandoc() as cmd module's do_help() does not de-indent docstrings by default.
                    doc = cleandoc(getattr(self, 'do_' + arg).__doc__)
                    if doc:
                        self.stdout.write("%s\n" % str(doc))
                        return
                except AttributeError:
                    pass
                self.stdout.write("%s\n" % str(self.nohelp % (arg,)))
                return
            func()
        else:
            names = self.get_names()
            cmds_doc = []
            cmds_undoc = []
            _help = {}
            for name in names:
                if name[:5] == 'help_':
                    _help[name[5:]] = 1
            names.sort()
            # There can be duplicates if routines overridden
            prevname = ''
            for name in names:
                if name[:3] == 'do_':
                    if name == prevname:
                        continue
                    prevname = name
                    _cmd = name[3:]
                    if _cmd in _help:
                        cmds_doc.append(_cmd)
                        del _help[_cmd]
                    elif getattr(self, name).__doc__:
                        cmds_doc.append(_cmd)
                    else:
                        cmds_undoc.append(_cmd)
            self.stdout.write("%s\n" % str(self.doc_leader))
            self.print_topics(self.doc_header, cmds_doc, 15, 80)
            self.print_topics(self.misc_header, list(_help.keys()), 15, 80)
            self.print_topics(self.undoc_header, cmds_undoc, 15, 80)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    courses = mmlsattendance.Courses()
    scraper = mmlsattendance.Scraper()
    prompt = Prompt()
    prompt.prompt = '> '
    prompt.ruler = '—'
    prompt.intro = cleandoc("""
          _____ _____ __    _____                   
         |     |     |  |  |   __|   .\\\\To start:
         | | | | | | |  |__|__   |    > guided
         |_|_|_|_|_|_|_____|_____|_
         |  _  | |_| |_ ___ ___ _| |___ ___ ___ ___
         |     |  _|  _| -_|   | . | .'|   |  _| -_|
         |__|__|_| |_| |___|_|_|___|__,|_|_|___|___|
         |   __|___ ___ ___ ___ ___ ___
         |__   |  _|  _| .'| . | -_|  _|
         |_____|___|_| |__,|  _|___|_|
                           |_|
         
         Enter 'help' or '?' to list commands.
    """)
    try:
        with open("attendance_cache.json", "r+") as file:
            scraper.attendance_cache = file
    except FileNotFoundError:
        pass
    try:
        prompt.cmdloop()
    except KeyboardInterrupt:
        print()
        prompt.do_exit("")
    finally:
        if os.path.exists("attendance_cache.json"):
            os.remove("attendance_cache.json")
        with open("attendance_cache.json", "w") as file:
            file.write(scraper.attendance_cache_json())
