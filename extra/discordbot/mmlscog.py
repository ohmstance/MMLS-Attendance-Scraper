from datetime import date, datetime, timedelta
from io import StringIO
from discord.ext import tasks, commands
import discord
import mmlsattendance
import aiohttp
import asyncio
import json as ijson
import re
import random
import os
import atexit

### CHECKS ###

class NoDirectMessage(commands.CheckFailure):
    pass

class NotLoggedIn(commands.CheckFailure):
    pass

class NoConcurrentCommands(commands.CheckFailure):
    pass

def is_not_dm():
    "Checks if invoked from a direct message."
    def predicate(ctx):
        if ctx.guild is None:
            raise NoDirectMessage(f'I know you want to keep it personal, but no DMs. Sorry.')
        return True
    return commands.check(predicate)

def is_logged_in(*, bypass_subcommands = {}):
    "Checks if user data is stored in usersdb."
    def predicate(ctx):
        [invoked_subcommand] = ctx.message.content[len(ctx.prefix):].split()[1:2] or [None]
        if ctx.invoked_with == 'help' or invoked_subcommand in bypass_subcommands:
            return True
        if not usersdb.get(ctx.author.id):
            raise NotLoggedIn(f'{ctx.author.mention} Log in first, will you?')
        return True
    return commands.check(predicate)

def is_not_running_command(*, bypass_subcommands = {}):
    """Checks if user is already running a command. If not, assigns current task
    (or command) to users_task and returns True."""
    def predicate(ctx):
        [invoked_subcommand] = ctx.message.content[len(ctx.prefix):].split()[1:2] or [None]
        if ctx.invoked_with == 'help' or invoked_subcommand in bypass_subcommands:
            return True
        task = users_task.get(ctx.author.id, None)
        if task is not None and not (task.done() or task.cancelled()): # If user has a task running
            raise NoConcurrentCommands(f"Wuh-wha? I'm busy processing your request, {ctx.author.mention}. "
                "I need to complete it first... unless you want me to cancel it?")
        users_task.update({ctx.author.id: asyncio.current_task()})
        return True
    return commands.check(predicate)

def check_failer():
    def predicate(ctx):
        return False
    return commands.check(predicate)

### CHECKS END ###
### FUNCTIONS ###

async def printer(ctx, queue):
    while True:
        f = await queue.get()
        embed = discord.Embed(
            title=f"{f.subject_code} - {f.subject_name}",
            url = f.attendance_url, colour=discord.Colour(0x807ec7),
            description=(
                f"{f.class_code} | {f.class_date} | {f.start_time[:-3]}-{f.end_time[:-3]} "
                f"| @{ctx.author.name}#{ctx.author.discriminator}"
                )
            )
        await ctx.send(embed=embed)
        queue.task_done()

def change_selection(SubjectDB_obj, args, op):
    """Parses selection command arguments, creates a dict of sets -- where
    key is subject index and value is a set of class index -- and iterates
    through each item and its set elements which through it does select,
    deselect or toggle operation to classes at their index."""
    args_list = args.lower().split() # E.g. ['1ab', '2ac', '3', '4a']
    if args_list and args_list[0] == 'all':
        for kelas in SubjectDB_obj.classes:
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
        subjects = SubjectDB_obj.subjects
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
                        x = op if op is not None else not subjects[sub_idx].classes[cls_idx].selected
                        subjects[sub_idx].classes[cls_idx].selected = x
                    except IndexError:
                        pass
    return True

### FUNCTIONS END ###

class MMLS(commands.Cog, name='mmls'):
    def __init__(self, bot):
        self.bot = bot

    def cog_check(self, ctx):
        if ctx.guild is None:
            raise NoDirectMessage(f'I know you want to keep it personal, but no DMs. Sorry.')
        return True

    @commands.command()
    @is_not_running_command()
    async def login(self, ctx, student_id):
        ("\nLoad registered subjects and classes from MMLS\n"
        "Note: Command logs out from MMLS after finishing and doesn't store your password\n")
        temp_subject_db = mmlsattendance.SubjectDB()
        dm_channel = ctx.author.dm_channel or await ctx.author.create_dm()
        ### Prompt user to see DM and introduce itself ###
        await dm_channel.send(f"I'm an attendance scraping bot from <#{ctx.channel.id}>. "
            "Reply with your MMLS password, or 'cancel' to cancel login.")
        await ctx.send(f'DM me your password {ctx.author.mention}.')
        for i in range(3):
            try:
                message = await self.bot.wait_for('message', check=lambda m: m.channel == dm_channel, timeout=300)
            except asyncio.TimeoutError:
                ### User timed-out entering their password ###
                await ctx.send(f"{ctx.author.mention} ghosted me. Goodbye anyways :(")
                return
            if message.content.lower() == 'cancel':
                ### Responds to user cancelling login ###
                await dm_channel.send(f"Changed your mind, huh? Oh well.")
                return
            async with ctx.channel.typing():
                ### Ask user to wait ###
                await dm_channel.send(f"Gon' make some network requests. Please wait for a bit...")
                try:
                    successful = await mmlsattendance.load_online(temp_subject_db, student_id, message.content, connector=connector)
                except asyncio.TimeoutError:
                    ### Network timeout ###
                    await dm_channel.send(f"I timed-out trying to get your courses. Sorry, but try again later.")
                    await ctx.send(f"Network timeout in getting {ctx.author.mention}'s courses.")
                    return
                if successful:
                    break
                elif i == 2:
                    ### Too many login attempts ###
                    await dm_channel.send(f"Try to remember your password. Poke me again once you do.")
                    await ctx.send(f"{ctx.author.mention} forgot their password lol.")
                    return
                else:
                    ### Invalid student password entered ###
                    await dm_channel.send(f"Check your password or student ID. Now, try again.")
        async with ctx.channel.typing():
            await mmlsattendance.autoselect_classes(temp_subject_db, student_id, connector=connector)
            if temp_subject_db.selected_classes:
                if not usersdb.get(ctx.author.id):
                    usersdb.add(ctx.author.id, student_id)
                usersdb.get(ctx.author.id).subject_db = temp_subject_db
                ### Log-in done! ###
                await dm_channel.send(f"You can go back to <#{ctx.channel.id}> now.")
                await ctx.send(f"{ctx.author.mention}, your registered subjects are loaded.")
            else:
                ### Somehow classes aren't selected ###
                await ctx.send(f"Huh? I'm having trouble with parsing registered classes. {ctx.author.mention}, try again in a bit.")

    @commands.group(name='search')
    @is_logged_in()
    @is_not_running_command()
    async def search(self, ctx):
        ("\n"
        "Search for attendance links in a specified range\n"
        "————————————————————————————————————————————————\n"
        "Syntax:   search date <start_date> <end_date>   \n"
        "          search date <date>                    \n"
        "          ...if date is empty, uses current date\n"
        "          search timetable <start_id> <end_id>  \n\n"
        "Examples: search date 2020-04-20 2020-08-31     \n"
        "          search date 2020-07-04                \n"
        "          search timetable 66666 69420          \n")
        if ctx.invoked_subcommand is None:
            await ctx.send(f"I don't... eh? What exactly do you want me do, {ctx.author.mention}?")

    ### SEARCH DATE|TIMETABLE COMMAND ###

    @search.command(name='date')
    async def _date(self, ctx, start_date=None, end_date=None):
        ("\nSearch by isoformat date\n")
        subject_db = usersdb.get(ctx.author.id).subject_db
        try:
            if start_date is None:
                start_date = end_date = (datetime.utcnow()+timedelta(hours=8)).date()
            elif end_date is None:
                start_date = end_date = date.fromisoformat(start_date)
            else:
                start_date, end_date = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except ValueError as err:
            await ctx.send(f"{ctx.author.mention}, {str(err).lower()}.") # Error can be caused by invalid isoformat string and invalid day for the month
        if start_date != end_date:
            await ctx.send(f"Looking for attendances from {start_date.isoformat()} to {end_date.isoformat()}.")
        else:
            await ctx.send(f"Looking for attendances in {start_date.isoformat()}.")
        async def scrape_and_print():
            queue = asyncio.Queue()
            printer_task = asyncio.create_task(printer(ctx, queue))
            await mmlsattendance.scrape_date(subject_db, start_date, end_date, queue=queue, connector=connector)
            await queue.join()
            printer_task.cancel()
            await asyncio.wait([printer_task])
        async with ctx.channel.typing():
            await scrape_and_print()
        await ctx.send(f"Hey, {ctx.author.mention}. I'm done searching.")

    @search.command()
    async def timetable(self, ctx, start_timetable, end_timetable):
        ("\nSearch by timetable ID range\n")
        subject_db = usersdb.get(ctx.author.id).subject_db
        try:
            start_timetable, end_timetable = int(start_timetable), int(end_timetable)
        except ValueError:
            ctx.send(f"Hunh... I need integers. Can I have integers with that command, {ctx.author.mention}?")
        await ctx.send(f"Now searching for attendances from {start_timetable} to {end_timetable}.")
        async def scrape_and_print():
            queue = asyncio.Queue()
            printer_task = asyncio.create_task(printer(ctx, queue))
            await mmlsattendance.scrape(subject_db, start_timetable, end_timetable, queue=queue, connector=connector)
            await queue.join()
            printer_task.cancel()
            await asyncio.wait([printer_task])
        async with ctx.channel.typing():
            await scrape_and_print()
        await ctx.send(f"Alrighty, {ctx.author.mention}. That's all that I have found.")

    ### SEARCH DATE|TIMETABLE COMMAND END ###

    @commands.command(name='users')
    async def _users(self, ctx):
        ("\nList users whose subjects are stored in memory\n")
        if usersdb.users:
            printable = "Subjects belonging to these users are stored:"
            for idx, user in enumerate(usersdb.users):
                printable += f"\n{idx+1}. <@{user.discord_id}>"
            await ctx.send(printable)
        else:
            await ctx.send(f"There are no users in store.")

    @commands.group(invoke_without_command=True)
    async def cancel(self, ctx):
        ("\nCancel a currently running command\n")
        task = users_task.get(ctx.author.id, None)
        if task is not None and not (task.done() or task.cancelled()):
            task.cancel()
            await ctx.send(f"{ctx.author.mention}, your current pending command is cancelled at your behest.")
        else:
            await ctx.send(f"You don't have any pending command, {ctx.author.mention}.")

    @cancel.command(name='all')
    @commands.is_owner()
    async def cancel_all(self, ctx):
        ("\nCancel all users' currently running command\n")
        is_running_anything = next((True for task in users_task.values() if not (task.done() or task.cancelled())), False)
        if is_running_anything:
            for task in users_task.values():
                if task is not None:
                    task.cancel()
            users_task.clear()
            await ctx.send(f"Hey! I'm doing something-- oh. Fine, {ctx.author.mention}, I'll stop.")
        else:
            await ctx.send(f"Huh? Okay, {ctx.author.mention}. That's easy, because I'm not doing anything right now.")

    @commands.group(invoke_without_command=True)
    @is_logged_in()
    @is_not_running_command()
    async def logout(self, ctx):
        ("\nRemove subjects and classes from memory\n")
        usersdb.remove(ctx.author.id)
        if users_task.get(ctx.author.id, None):
            del users_task[ctx.author.id]
        if random.randint(1, 10) > 9:
            await ctx.send(f"Nope. Your password is mine, {ctx.author.mention}.")
            await asyncio.sleep(5)
            await ctx.send(f"Just kidding -- passwords aren't stored. {ctx.author.mention}, your subjects are deleted from memory.")
        else:
            await ctx.send(f"Data banks pertaining {ctx.author.mention} are purged.")

    @logout.command(name='all')
    @commands.is_owner()
    async def logout_all(self, ctx):
        ("\nRemove all users' subjects from memory\n")
        if not usersdb.users:
            await ctx.send(f"Ah, okay. I'll get right to it-- wait. This is funny. Nobody logged in, {ctx.author.mention}.")
            return
        usersdb.remove_all()
        await ctx.send(f"Burn baby burn. All data pertaining users' subjects and classes are purged.")

    @logout.error
    async def logout_error_handler(self, ctx, error):
        if isinstance(error, NotLoggedIn):
            await ctx.send(f"You weren't even logged in... {ctx.author.mention}")
        else:
            raise

    @commands.group(name='print', aliases=['print_subjects'], invoke_without_command=True)
    @is_logged_in()
    async def print_subjects(self, ctx):
        ("\nDisplay stored subjects, classes and selection\n")
        subject_db = usersdb.get(ctx.author.id).subject_db
        with StringIO() as f:
            num_pad = len(str(len(subject_db.subjects)))
            space = ' '*num_pad + ' '*2
            cat_space = space + ' '*3
            for subject_no, subject in enumerate(subject_db.subjects, 1):
                subj_no = str(subject_no).rjust(num_pad)
                print(f"{subj_no}. {subject.code} - {subject.name}", file=f)        # 1. ECE2056 - DATA COMM AND NEWORK
                print(f"{space}> Subject ID: {subject.id}", file=f)                 #    > Subject ID: 232
                print(f"{space}> Coordinator ID: {subject.coordinator_id}", file=f) #    > Coordinator ID: 1577623541
                print(f"{cat_space}Sel Class Class ID", file=f)                     #       Sel Class Class ID
                for char_id, kelas in enumerate(subject.classes, ord('a')):         #    a. [X]  EC01    45132
                    X = 'X' if kelas.selected else ' '                              #    b. [ ]  ECA1    45172
                    print(f"{space}{chr(char_id)}. [{X}]{kelas.code:>6}{kelas.id:>9}", file=f)
                if subject_no != len(subject_db.subjects):
                    print('', file=f)
            await ctx.send(f"Here's your registered subjects and their classes:\n```{f.getvalue()}```")

    @print_subjects.command(name='user')
    @commands.is_owner()
    async def print_subjects_user(self, ctx, user: discord.User):
        ("\nDisplay a mentioned user's subjects, classes and selection\n")
        if not usersdb.get(user.id):
            await ctx.send(f"The user isn't logged in, {ctx.author.mention}.")
            return
        subject_db = usersdb.get(user.id).subject_db
        with StringIO() as f:
            num_pad = len(str(len(subject_db.subjects)))
            space = ' '*num_pad + ' '*2
            cat_space = space + ' '*3
            for subject_no, subject in enumerate(subject_db.subjects, 1):
                subj_no = str(subject_no).rjust(num_pad)
                print(f"{subj_no}. {subject.code} - {subject.name}", file=f)        # 1. ECE2056 - DATA COMM AND NEWORK
                print(f"{space}> Subject ID: {subject.id}", file=f)                 #    > Subject ID: 232
                print(f"{space}> Coordinator ID: {subject.coordinator_id}", file=f) #    > Coordinator ID: 1577623541
                print(f"{cat_space}Sel Class Class ID", file=f)                     #       Sel Class Class ID
                for char_id, kelas in enumerate(subject.classes, ord('a')):         #    a. [X]  EC01    45132
                    X = 'X' if kelas.selected else ' '                              #    b. [ ]  ECA1    45172
                    print(f"{space}{chr(char_id)}. [{X}]{kelas.code:>6}{kelas.id:>9}", file=f)
                if subject_no != len(subject_db.subjects):
                    print('', file=f)
            await ctx.send(f"Here's their registered subjects and classes:\n```{f.getvalue()}```")

    @commands.command()
    @is_logged_in()
    @is_not_running_command()
    async def autoselect(self, ctx):
        ("\nAuto-select classes that the student has registered\n")
        subject_db = usersdb.get(ctx.author.id).subject_db
        await mmlsattendance.autoselect_classes(subject_db, usersdb.get(ctx.author.id).student_id, connector=connector)
        await ctx.send(f"{ctx.author.mention}, your registered classes are now selected.")

    @commands.command()
    @is_logged_in()
    @is_not_running_command()
    async def select(self, ctx, *, args):
        ("\n"
        "Add selection to classes       \n"
        "———————————————————————————————\n"
        "Examples: select 1a 2c 3 4abc 5\n"
        "          select all           \n")
        subject_db = usersdb.get(ctx.author.id).subject_db
        if not change_selection(subject_db, args, True):
            await ctx.send(f"{ctx.author.mention} Invalid command.")

    @commands.command()
    @is_logged_in()
    @is_not_running_command()
    async def deselect(self, ctx, *, args):
        ("\n"
        "Remove selection in classes      \n"
        "—————————————————————————————————\n"
        "Examples: deselect 1a 2c 3 4abc 5\n"
        "          deselect all           \n")
        subject_db = usersdb.get(ctx.author.id).subject_db
        if not change_selection(subject_db, args, False):
            await ctx.send(f"{ctx.author.mention} Invalid command.")

    @commands.command()
    @is_logged_in()
    @is_not_running_command()
    async def toggle(self, ctx, *, args):
        ("\n"
        "Toggle selection of classes    \n"
        "———————————————————————————————\n"
        "Examples: toggle 1a 2c 3 4abc 5\n"
        "          toggle all           \n")
        subject_db = usersdb.get(ctx.author.id).subject_db
        if not change_selection(subject_db, args, None):
            await ctx.send(f"{ctx.author.mention} Invalid command.")

    @commands.group()
    @commands.is_owner()
    async def caching(self, ctx):
        ("\n"
        "Interact with the caching service                       \n"
        "————————————————————————————————————————————————————————\n"
        "Syntax:   caching <status|start|stop|resume|suspend|run>\n")
        if ctx.invoked_subcommand is None:
            await ctx.send(f"I don't... eh? What exactly do you want me do, {ctx.author.mention}?")

    @caching.command(name='status')
    async def caching_status(self, ctx):
        ("\nDisplay caching service status\n")
        caching_cog = self.bot.get_cog('Caching')
        if caching_cog:
            await ctx.send(
                f"Total cached: {mmlsattendance.total_cached_timetable_ids()}\n"
                f"Last run: {caching_cog.last_cache_run} UTC\n"
                f"Last run difference: {caching_cog.last_cache_difference}\n"
                f"Caching task is {'running' if caching_cog.is_running() else 'suspended'}, {ctx.author.mention}."
                )
        else:
            await ctx.send(
                f"Total cached: {mmlsattendance.total_cached_timetable_ids()}\n"
                f"Caching service wasn't started, {ctx.author.mention}.\n"
                )

    @caching.command(name='start')
    async def caching_start(self, ctx):
        ("\nStart caching service\n")
        caching_cog = self.bot.get_cog('Caching')
        if not caching_cog:
            self.bot.add_cog(Caching(self.bot))
            await ctx.send(f"Caching service started, {ctx.author.mention}.")
        else:
            await ctx.send(f"Caching service is already started, {ctx.author.mention}.")

    @caching.command(name='stop')
    async def caching_stop(self, ctx):
        ("\nStop caching service\n")
        caching_cog = self.bot.get_cog('Caching')
        if caching_cog:
            self.bot.remove_cog('Caching')
            await ctx.send(f"Caching service stopped, {ctx.author.mention}.")
        else:
            await ctx.send(f"Caching service is already stopped, {ctx.author.mention}.")

    @caching.command(name='resume')
    async def caching_resume(self, ctx):
        ("\nResume caching service\n")
        caching_cog = self.bot.get_cog('Caching')
        if caching_cog and not caching_cog.is_running():
            caching_cog.resume_service()
            await ctx.send(f"Caching service resumed, {ctx.author.mention}.")
        elif caching_cog:
            await ctx.send(f"Caching service is already running, {ctx.author.mention}.")
        else:
            await ctx.send(f"Caching service wasn't started, {ctx.author.mention}.")

    @caching.command(name='suspend')
    async def caching_suspend(self, ctx):
        ("\nSuspend caching service\n")
        caching_cog = self.bot.get_cog('Caching')
        if caching_cog and caching_cog.is_running():
            await caching_cog.suspend_service()
            await ctx.send(f"Caching service suspended, {ctx.author.mention}.")
        elif caching_cog:
            await ctx.send(f"Caching service is already suspended, {ctx.author.mention}.")
        else:
            await ctx.send(f"Caching service wasn't started, {ctx.author.mention}.")

    @caching.command(name='run')
    async def caching_run(self, ctx):
        ("\nTrigger caching to run now\n")
        caching_cog = self.bot.get_cog('Caching')
        was_running = caching_cog.is_running()
        if caching_cog:
            await caching_cog.run_now()
            await ctx.send(f"Caching manually invoked, {ctx.author.mention}.")
        else:
            await ctx.send(f"Caching service wasn't started, {ctx.author.mention}.")

    async def cog_command_error(self, ctx, error):
        """The event triggered when an error is raised while invoking a command
        inside cog.
        Parameters
        ------------
        ctx: commands.Context
            The context used for command invocation.
        error: commands.CommandError
            The Exception raised.
        """
        if hasattr(ctx.command, 'on_error'): # If command has local error handler, it won't be handled.
            return
        ignored = (commands.CommandNotFound, )
        error = getattr(error, 'original', error) # Check original exception raised if not found use current error
        if isinstance(error, ignored): # Ignores exceptions in 'ignored'
            return
        if isinstance(error, NoDirectMessage):
            await ctx.send(error)
        elif isinstance(error, NotLoggedIn):
            await ctx.send(error)
        elif isinstance(error, NoConcurrentCommands):
            await ctx.send(error)
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"I think you left out {error.param.name}, {ctx.author.mention}.")
        elif isinstance(error, commands.NotOwner):
            await ctx.send(f"Why are you fiddling with that command, {ctx.author.mention}? It's reserved for my owner.")
        elif isinstance(error, aiohttp.ClientConnectorError):
            print(f'{datetime.now().isoformat()} Ignoring exception in command {ctx.command} -> {error.__class__.__name__}: {error}')
            if 'mmls.mmu.edu.my' in error.host:
                await ctx.send(f"Hell, I'm having trouble connecting to MMLS. Try again later, {ctx.author.mention}.")
            else:
                await ctx.send(f"Hm? Some network error occured. Try again later, {ctx.author.mention}, or maybe contact my owner.")
        elif isinstance(error, mmlsattendance.MMLSResponseError):
            print(f'{datetime.now().isoformat()} Ignoring exception in command {ctx.command} -> {error.__class__.__name__}: {error}')
            await ctx.send(f"Is MMLS okay? Because its HTTP status is not OK. Try again later, {ctx.author.mention}.")
        elif isinstance(error, aiohttp.ClientError):
            print(f'{datetime.now().isoformat()} Ignoring exception in command {ctx.command} -> {error.__class__.__name__}: {error}')
            await ctx.send(f"I seem to be having network problems. Try again later, {ctx.author.mention}, or maybe contact my owner.")
        else:
            print(f'{datetime.now().isoformat()} Ignoring exception in command {ctx.command} -> {error.__class__.__name__}: {error}')

class Caching(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.new_cache_count = 0
        self.last_cache_run = None
        self.last_cache_difference = 0
        print(f'{datetime.now().isoformat()} Caching service started.')
        self.resume_service()

    def cog_unload(self):
        self.cache_attendances_scheduler.cancel()
        self.cache_attendances.cancel()
        print(f'{datetime.now().isoformat()} Caching service stopped.')

    def is_running(self):
        return self.cache_attendances_scheduler.is_running() or self.cache_attendances.is_running()

    def resume_service(self):
        self.cache_attendances_scheduler.start()
        print(f'{datetime.now().isoformat()} Caching service is running.')

    async def suspend_service(self):
        self.cache_attendances.cancel()
        self.cache_attendances_scheduler.cancel()
        while self.is_running():
            await asyncio.sleep(0.1)
        print(f'{datetime.now().isoformat()} Caching service suspended.')

    async def run_now(self):
        print(f'{datetime.now().isoformat()} Caching manually invoked.')
        was_running = self.is_running()
        self.cache_attendances.cancel()
        self.cache_attendances_scheduler.cancel()
        while self.is_running():
            await asyncio.sleep(0.1)
        self.cache_attendances_scheduler.start()

    def seconds_until_next_mst_day(self):
        mst_now = datetime.utcnow()+timedelta(hours=8) # current datetime at UTC+8 or MST
        inc_mst = mst_now+timedelta(days=1)
        next_day_mst = datetime(inc_mst.year, inc_mst.month, inc_mst.day) # datetime object of next day at 0000 hours MST
        return (next_day_mst-mst_now).seconds # Seconds until next day in MST

    @tasks.loop(hours=1) # placeholder time
    async def cache_attendances_scheduler(self):
        """Runs 15 minutes after a new MST day and loop is reset to run caching every 2 hours.
        If new cache is added three times, or was added once but nothing is added now, or it's PM, runs every 6 hours.
        """
        self.cache_attendances.cancel()
        while self.cache_attendances.is_running():
            await asyncio.sleep(0.1)
        if self.last_cache_run is not None:
            last_run_date = (self.last_cache_run+timedelta(hours=8)).date() # MST
            now_date = ((datetime.utcnow()+timedelta(hours=8))).date() # MST
            if (now_date-last_run_date).days > 0:
                self.cache_attendances.change_interval(hours=2)
                self.new_cache_count = 0
        self.cache_attendances.start()
        self.cache_attendances_scheduler.change_interval(seconds=self.seconds_until_next_mst_day(), minutes=15)

    @tasks.loop(hours=2)
    async def cache_attendances(self):
        print(f'{datetime.now().isoformat()} Caching started.')
        initial_cached_count = mmlsattendance.total_cached_timetable_ids()
        final_cached_count = await mmlsattendance.cache_timetable_ids(connector=connector)
        cached_count_difference = final_cached_count-initial_cached_count
        if cached_count_difference:
            self.new_cache_count += 1
        if (
            self.new_cache_count >= 3
            or (not cached_count_difference and self.new_cache_count)
            or self.seconds_until_next_mst_day() <= timedelta(hours=12).seconds
            ):
            self.cache_attendances.change_interval(hours=6)
        self.last_cache_run = datetime.utcnow()
        self.last_cache_difference = cached_count_difference
        print(f'{datetime.now().isoformat()} Caching ended with {final_cached_count-initial_cached_count} changes.')

    @cache_attendances.before_loop
    async def before_cache_attendances(self):
        await self.bot.wait_until_ready()

class Users:
    _users = {}

    @property
    def users(self):
        return [user for user in self._users.values()]

    def get(self, discord_id):
        return self._users.get(discord_id, None)

    def add(self, discord_id, student_id):
        self._users[discord_id] = self.User(discord_id, student_id)

    def remove(self, discord_id):
        try:
            self._users.pop(discord_id, None).stop_command()
        except (ValueError, AttributeError):
            pass

    def remove_all(self):
        self._users = {}

    def json(self):
        users = {
            user.discord_id:
                {
                'student_id': user.student_id,
                'subject_db': ijson.loads(user.subject_db.json())
                } for user in self._users.values()
            }
        return ijson.dumps(users)

    def load_json(self, json_str):
        users_jsonable = ijson.loads(json_str) # jsonable but it's not in json str format my dumb brain!
        users = {}
        for discord_id, stud_id_sub_db_dict in users_jsonable.items():
            student_id = stud_id_sub_db_dict['student_id']
            subject_db_jsonable = stud_id_sub_db_dict['subject_db']
            User_obj = self.User(int(discord_id), int(student_id))
            User_obj.subject_db.load_json(ijson.dumps(subject_db_jsonable))
            users.update({int(discord_id): User_obj})
        self._users = users

    class User:
        def __init__(self, discord_id, student_id, *, subject_db=None):
            self._discord_id = discord_id
            self._student_id = student_id
            self._subject_db = subject_db or mmlsattendance.SubjectDB()

        @property
        def discord_id(self):
            return self._discord_id

        @property
        def student_id(self):
            return self._student_id

        @property
        def subject_db(self):
            return self._subject_db

        @subject_db.setter
        def subject_db(self, value):
            if isinstance(value, mmlsattendance.SubjectDB):
                self._subject_db = value
            else:
                raise TypeError('Expected mmlsattendance.SubjectDB object!')

usersdb = Users()
users_task = {}
connector = aiohttp.TCPConnector(limit=8)

try:
    with open('users.json', 'r+') as f:
        usersdb.load_json(f.read())
except FileNotFoundError:
    pass

@atexit.register
def save_usersdb():
    if os.path.exists('users.json'):
        os.remove('users.json')
    with open('users.json', 'w') as f:
        f.write(usersdb.json())

def setup(bot):
    bot.add_cog(MMLS(bot))
    bot.add_cog(Caching(bot))

def teardown(bot):
    users_task = {}
    asyncio.get_event_loop().run_until_complete(connector.close())
