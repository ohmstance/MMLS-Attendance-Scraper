from datetime import date, datetime, timedelta
from discord.ext import commands
from io import StringIO
import mmlsattendance
import discord
import asyncio
import re
import os

help_command = commands.DefaultHelpCommand(no_category = 'General commands')
bot = commands.Bot(command_prefix=commands.when_mentioned_or(), help_command=help_command)
glob_semaphore = asyncio.Semaphore(6)
userid_to_subjectdb = {}

def print_subjects(SubjectDB_obj):
    f = StringIO()
    num_pad = len(str(len(SubjectDB_obj.subjects)))
    space = ' '*num_pad + ' '*2
    cat_space = space + ' '*3
    for subject_no, subject in enumerate(SubjectDB_obj.subjects, 1):
        subj_no = str(subject_no).rjust(num_pad)
        print(f"{subj_no}. {subject.code} - {subject.name}", file=f)        # 1. ECE2056 - DATA COMM AND NEWORK
        print(f"{space}> Subject ID: {subject.id}", file=f)                 #    > Subject ID: 232
        print(f"{space}> Coordinator ID: {subject.coordinator_id}", file=f) #    > Coordinator ID: 1577623541
        print(f"{cat_space}Sel Class Class ID", file=f)                     #       Sel Class Class ID
        for char_id, kelas in enumerate(subject.classes, ord('a')):         #    a. [X]  EC01    45132
            X = 'X' if kelas.selected else ' '                              #    b. [ ]  ECA1    45172
            print(f"{space}{chr(char_id)}. [{X}]{kelas.code:>6}{kelas.id:>9}", file=f)
        if subject_no != len(SubjectDB_obj.subjects):
            print()
    return f

async def printer(ctx, queue, found_dates=set()):
    while True:
        f = await queue.get()
        embed = discord.Embed(title=f"{f.subject_code} - {f.subject_name}",
                             url = f.attendance_url, colour=discord.Colour(0x807ec7),
                             description=f"{f.class_code} | {f.class_date} | {f.start_time}-{f.end_time} | @{ctx.author.name}")
        await ctx.channel.send(embed=embed)
        found_dates.add(date.fromisoformat(f.class_date))
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
                        subjects[sub_idx].classes[cls_idx].selected = op if op is not None else not subjects[sub_idx].classes[cls_idx].selected
                    except IndexError:
                        pass
    return True

@bot.check
async def globally_block_dms(ctx):
    return ctx.guild is not None

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    for guild_num, guild in enumerate(bot.guilds, 1):
        print(f"-> {guild.id}: {guild.name}")
    print(f"Bot is in {guild_num} guilds.")

@bot.command()
async def login(ctx, stud_id):
    ("\nLoads registered subjects and classes from MMLS.\n"
    "Note: Command logs out from MMLS after finishing and doesn't store your password.\n")
    subjectdb = mmlsattendance.SubjectDB()
    dm_channel = ctx.author.dm_channel or await ctx.author.create_dm()
    await dm_channel.send(f"I'm an attendance scraping bot from <#{ctx.channel.id}>. Reply with your MMLS password, or 'cancel' to cancel login.")
    await ctx.channel.send(f'DM me your password {ctx.author.mention}.')
    try:
        for i in range(3):
            message = await bot.wait_for('message', check = lambda m: m.channel == dm_channel, timeout=300)
            if message.content.lower() == 'cancel':
                await dm_channel.send(f"Changed your mind, huh? Oh well.")
                return
            await dm_channel.send(f"Gon' make some network requests. Please wait for a bit...")
            async with ctx.channel.typing():
                if await mmlsattendance.load_online(subjectdb, stud_id, message.content, semaphore = glob_semaphore):
                    break
                elif i == 2:
                    await dm_channel.send(f"Try to remember your password. Poke me again once you do.")
                    await ctx.channel.send(f"{ctx.author.mention} forgot their password lol.")
                    return
                else:
                    await dm_channel.send(f"Check your password or student ID. Now, try again.")
                    continue
    except asyncio.TimeoutError:
        await ctx.channel.send(f"{ctx.author.mention} ghosted me. Goodbye anyways :(")
        return
    async with ctx.channel.typing():
        await mmlsattendance.autoselect_classes(subjectdb, stud_id)
        if subjectdb.selected_classes:
            userid_to_subjectdb.update({ctx.author.id: {'StudentID': stud_id}})
            userid_to_subjectdb[ctx.author.id].update({'SubjectDB': subjectdb})
            await dm_channel.send(f"You can go back to <#{ctx.channel.id}> now.")
            await ctx.channel.send(f"{ctx.author.mention}, your registered subjects are loaded.")
        else:
            await ctx.channel.send(f"I'm having trouble with parsing registered classes. I think I might be rate-limited. {ctx.author.mention}, try again in a bit.")

@bot.command(name='print')
async def print_subjects(ctx):
    ("""\nDisplay stored subjects, classes and selection.\n""")
    if ctx.author.id not in userid_to_subjectdb:
        await ctx.channel.send(f"Log in first will you? {ctx.author.mention}")
        return
    SubjectDB_obj = userid_to_subjectdb[ctx.author.id]['SubjectDB']
    with StringIO() as f:
        num_pad = len(str(len(SubjectDB_obj.subjects)))
        space = ' '*num_pad + ' '*2
        cat_space = space + ' '*3
        for subject_no, subject in enumerate(SubjectDB_obj.subjects, 1):
            subj_no = str(subject_no).rjust(num_pad)
            print(f"{subj_no}. {subject.code} - {subject.name}", file=f)        # 1. ECE2056 - DATA COMM AND NEWORK
            print(f"{space}> Subject ID: {subject.id}", file=f)                 #    > Subject ID: 232
            print(f"{space}> Coordinator ID: {subject.coordinator_id}", file=f) #    > Coordinator ID: 1577623541
            print(f"{cat_space}Sel Class Class ID", file=f)                     #       Sel Class Class ID
            for char_id, kelas in enumerate(subject.classes, ord('a')):         #    a. [X]  EC01    45132
                X = 'X' if kelas.selected else ' '                              #    b. [ ]  ECA1    45172
                print(f"{space}{chr(char_id)}. [{X}]{kelas.code:>6}{kelas.id:>9}", file=f)
            if subject_no != len(SubjectDB_obj.subjects):
                print('', file=f)
        await ctx.channel.send(f"Here's your registered subjects and their classes:\n```{f.getvalue()}```")

@bot.group(aliases=['search'])
async def scrape(ctx):
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
    if ctx.invoked_subcommand is None:
        await ctx.send(f"I don't... eh? What exactly do you want me do, {ctx.author.mention}?")

@scrape.command(name='date')
async def _date(ctx, start_date=None, end_date=None):
    if ctx.author.id not in userid_to_subjectdb:
        await ctx.channel.send(f"Log in first will you? {ctx.author.mention}")
        return
    SubjectDB_obj = userid_to_subjectdb[ctx.author.id]['SubjectDB']
    try:
        if start_date is None:
            start_date = end_date = (datetime.utcnow()+timedelta(hours=8)).date()
        elif end_date is None:
            start_date = end_date = date.fromisoformat(start_date)
        else:
            start_date, end_date = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError as err:
        await ctx.channel.send(f"{err}. {ctx.author.mention}, robots are dumb, so you'll have to format the dates in yyyy-mm-dd.")
    if start_date != end_date:
        await ctx.channel.send(f"Looking for attendances from {start_date.isoformat()} to {end_date.isoformat()}.")
    else:
        await ctx.channel.send(f"Looking for attendances in {start_date.isoformat()}.")
    found_dates = set()
    async def scrape_and_print():
        queue = asyncio.Queue()
        printer_task = asyncio.create_task(printer(ctx, queue, found_dates))
        await mmlsattendance.scrape_date(SubjectDB_obj, start_date, end_date, queue = queue, semaphore = glob_semaphore)
        await queue.join()
        printer_task.cancel()
        await asyncio.wait([printer_task])
    async with ctx.channel.typing():
        await scrape_and_print()
        for i_date in (start_date + timedelta(days=d) for d in range((end_date-start_date).days+1)):
            found_dates.discard(i_date)
    if not found_dates:
        await ctx.channel.send(f"Hey, {ctx.author.mention}. Scraping is finished, I guess.")
    else:
        await ctx.channel.send(("Oh dear. I've probably missed some attendance URLs. "
        f"Try using timetable ID range scraping instead, will you, {ctx.author.mention}? See '{bot.user.mention} help scrape'."))

@scrape.command()
async def timetable(ctx, start_timetable, end_timetable):
    if ctx.author.id not in userid_to_subjectdb:
        await ctx.channel.send(f"Log in first will you? {ctx.author.mention}")
        return
    SubjectDB_obj = userid_to_subjectdb[ctx.author.id]['SubjectDB']
    try:
        start_timetable, end_timetable = int(start_timetable), int(end_timetable)
    except TypeError:
        ctx.channel.send(f"Hunh... I need integers. Can I have integers with that command, {ctx.author.mention}?")
    await ctx.channel.send(f"Now searching for attendances from {start_timetable} to {end_timetable}.")
    async def scrape_and_print():
        queue = asyncio.Queue()
        printer_task = asyncio.create_task(printer(ctx, queue))
        await mmlsattendance.scrape(SubjectDB_obj, start_timetable, end_timetable, queue = queue, semaphore = glob_semaphore)
        await queue.join()
        printer_task.cancel()
        await asyncio.wait([printer_task])
    async with ctx.channel.typing():
        await scrape_and_print()
    await ctx.channel.send(f"Alrighty, {ctx.author.mention}. That's all that I have found.")

@timetable.error
async def scrape_timetable_handler(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        if error.param.name == 'start_timetable' or error.param.name == 'end_timetable':
            await ctx.channel.send(f"I think you left out something, {ctx.author.mention}. Maybe a missing argument?")
    else:
        raise

@bot.command()
async def status(ctx):
    ("\nList users whose subjects are stored in memory.\n")
    if userid_to_subjectdb:
        printable = "Subjects belonging to these users are stored:"
        for idx, userid in enumerate(userid_to_subjectdb.keys()):
            printable += f"\n{idx+1}. <@{userid}>"
        await ctx.channel.send(printable)
    else:
        await ctx.channel.send(f"There are no users in store.")

@bot.command()
async def logout(ctx):
    ("\nRemoves subjects and classes of the calling user from memory.\n")
    try:
        userid_to_subjectdb.pop(ctx.author.id)
        await ctx.channel.send(f"Data banks pertaining {ctx.author.mention} are purged.")
    except KeyError:
        await ctx.channel.send(f"You weren't even logged in... {ctx.author.mention}")

@bot.command()
async def select(ctx, *, args):
    ("\n"
    "Add selection to classes.      \n"
    "———————————————————————————————\n"
    "Examples: select 1a 2c 3 4abc 5\n"
    "          select all           \n")
    try:
        subjectdb = userid_to_subjectdb[ctx.author.id]['SubjectDB']
        if not change_selection(subjectdb, args, True):
            await ctx.channel.send(f"{ctx.author.mention} Invalid command.")
    except KeyError:
        await ctx.channel.send(f"Log in first will you? {ctx.author.mention}")

@bot.command()
async def deselect(ctx, *, args):
    ("\n"
    "Remove selection in classes.     \n"
    "—————————————————————————————————\n"
    "Examples: deselect 1a 2c 3 4abc 5\n"
    "          deselect all           \n")
    try:
        subjectdb = userid_to_subjectdb[ctx.author.id]['SubjectDB']
        if not change_selection(subjectdb, args, False):
            await ctx.channel.send(f"{ctx.author.mention} Invalid command.")
    except KeyError:
        await ctx.channel.send(f"Log in first will you? {ctx.author.mention}")

@bot.command()
async def toggle(ctx, *, args):
    ("\n"
    "Toggle selection of classes.   \n"
    "———————————————————————————————\n"
    "Examples: toggle 1a 2c 3 4abc 5\n"
    "          toggle all           \n")
    try:
        subjectdb = userid_to_subjectdb[ctx.author.id]['SubjectDB']
        if not change_selection(subjectdb, args, None):
            await ctx.channel.send(f"{ctx.author.mention} Invalid command.")
    except KeyError:
        await ctx.channel.send(f"Log in first will you? {ctx.author.mention}")

bot.run('insert your token here')
