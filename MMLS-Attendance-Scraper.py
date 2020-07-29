from urllib import request, error, parse
from lxml import etree
from datetime import date, datetime, timedelta
import concurrent.futures
import time
import getpass
import json

baseAttendanceURL = 'https://mmls.mmu.edu.my/attendance'
baseAttendanceListURL = 'https://mmls.mmu.edu.my/viewAttendance'
mmlsLoginURL = 'https://mmls.mmu.edu.my/checklogin?' #stud_id, stud_pswrd, _token. POST
mmlsAttendanceLoginURL= 'https://mmls.mmu.edu.my/attendancelogin?' #stud_id, stud_pswrd, timetable_id, starttime, endtime, class_date, class_id, _token. POST.
mmlsURL = 'https://mmls.mmu.edu.my/'                               #^^^Need Referer header: any attendance link.
mmlsClassListURL = 'https://mmls.mmu.edu.my/studentlist'
mmlsLogoutURL = 'https://mmls.mmu.edu.my/logout' #headers: cookie. GET
mobileLoginURL = 'https://mmumobileapps.mmu.edu.my/api/auth/login2' #username, password. POST
mobileSubjectListURL = 'https://mmumobileapps.mmu.edu.my/api/mmls/subject?' #token. GET
maxTimetableID = 99999
minTimetableID = 1
workers = 64
subjectListDB = []
semStartDate = None

def getURL(url, *, data={}, headers={}): #Sends GET request, returns HTTP response.
    data = parse.urlencode(data)
    req = request.Request(url+data, data=None, headers=headers, method='GET')
    return request.urlopen(req)

def postURL(url, *, data=None, headers={}): #Sends POST request, returns HTTP response.
    data = parse.urlencode(data).encode('utf-8') if data else None
    req = request.Request(url, data=data, headers=headers, method='POST')
    return request.urlopen(req)

def getAttendanceTree(timetableID): #Accepts timetable_id. Parses attendance HTTP response of input timetable_id. Returns ElementTree object, but None type if failed.
    try:
        html = request.urlopen(baseAttendanceURL+':0:0:'+str(timetableID), timeout=20)
        tree = etree.parse(html, etree.HTMLParser())
        return tree
    except error.HTTPError as err:
        if err.code == 500: return None

def dateToTimetableID(date, option, upperbound = maxTimetableID, lowerbound = minTimetableID):
    while(True): #Option: 1 for first occurence, -1 for last occurence; Binary search algorithm; Returns None if no class on that date.
        currTimetableID = (upperbound+lowerbound)//2
        html_etree = getAttendanceTree(currTimetableID)
        if not html_etree:
            upperbound = currTimetableID-1
            continue
        currDate = dateFromISOFormat(html_etree.xpath("//input[@name='class_date']/@value")[0])
        if (date - currDate).days > 0:
            lowerbound = currTimetableID+1
        elif (date - currDate).days < 0:
            upperbound = currTimetableID-1
        elif (date - currDate).days == 0 and option == 1:
            html_etree = getAttendanceTree(currTimetableID-1)
            if dateFromISOFormat(html_etree.xpath("//input[@name='class_date']/@value")[0]) != currDate:
                return currTimetableID
            upperbound = currTimetableID-1
        elif (date - currDate).days == 0 and option == -1:
            html_etree = getAttendanceTree(currTimetableID+1)
            if not html_etree:
                return currTimetableID
            if dateFromISOFormat(html_etree.xpath("//input[@name='class_date']/@value")[0]) != currDate:
                return currTimetableID
            lowerbound = currTimetableID+1
        if upperbound < lowerbound:
            return None

def askYesNo(question): #Accepts string -- preferably a question. Returns boolean result where y: True and n: False.
    while True:
        decision = input("{} (y/n): ".format(question))
        if (decision.lower() == 'y'): return True
        if (decision.lower() == 'n'): return False
        print("Invalid input.")

def printSubjectList(): #Prints all subjects in SubjectListDB prettily.
    for index, subject in enumerate(subjectListDB):
        print("{}. {} - {}".format(index+1, subject['subject_code'], subject['subject_name']))
        for class_ in subject['classes']:
            print('   [{}] {}'.format('X' if class_['selected'] else ' ', class_['class_name']))

def printSubject(subjectNo):
    print("{}. {} - {}".format(subjectNo+1, subjectListDB[subjectNo]['subject_code'], subjectListDB[subjectNo]['subject_name']))
    for classNo, class_ in enumerate(subjectListDB[subjectNo]['classes']):
        print('   [{}] {} ({})'.format('X' if class_['selected'] else ' ', class_['class_name'], classNo+1))

def editSubjectList(): #User interface for making class search selection in SubjectListDB.
    try:
        subjectNo = int(input('Select which subject?: '))-1
        print('')
        printSubject(subjectNo)
        classes = [int(classNo)-1 for classNo in input("\nToggle which classes?: ").split(' ')]
        for classNo in classes:
            subjectListDB[subjectNo]['classes'][classNo]['selected'] = not subjectListDB[subjectNo]['classes'][classNo]['selected']
    except (ValueError, IndexError):
        print('Invalid input.')

def dateFromISOFormat(ISODateString): #Similar to date.fromisoformat for use in Python versions prior 3.7
    dateList = ISODateString.split('-')
    if len(dateList[0]) != 4 or len(dateList[1]) != 2 or len(dateList[2]) != 2:
        raise ValueError("Invalid isoformat string: '{}'".format(ISODateString))
    return date(int(dateList[0]), int(dateList[1]), int(dateList[2]))

def parseClasses(subject, cookie): #Accepts subject dict in SubjectListDB and cookie for MMLS. Returns a list of class dicts.
    subjectClassListURL = mmlsClassListURL + ':' + subject['subject_id'] + ':' + subject['coordinator_id'] + ':0'
    response = getURL(subjectClassListURL, headers={'Cookie' : cookie})
    tree = etree.parse(response, etree.HTMLParser())
    classIDs = tree.xpath("//select[@id='select_class']/*[not(self::option[@value='0'])]/@value")
    classNames = tree.xpath("//select[@id='select_class']/*[not(self::option[@value='0'])]/text()")
    classList = [{'class_name' : class_[0], 'class_id' : class_[1], 'selected' : False} for class_ in zip(classNames, classIDs)]
    return classList

def userInClass(student_id, class_id): #Returns True if user in class, False otherwise.
    cookie = request.urlopen('https://mmls.mmu.edu.my/attendance:0:0:1').info()['Set-Cookie']
    data_params = {'stud_id' : student_id, 'stud_pswrd' : '0', 'class_id' : class_id}
    header_params = {'Cookie' : cookie, 'Referer' : 'https://mmls.mmu.edu.my/attendance:0:0:1'}
    response = postURL(mmlsAttendanceLoginURL, data=data_params, headers=header_params)
    tree = etree.parse(response, etree.HTMLParser())
    return False if tree.xpath("//div[@class='alert alert-danger']/text()='You are not register to this class.'") else True

def getMMLSCookieToken(): #I put this in fuction for async execution as this task does not need user-derived information
    response = request.urlopen(mmlsURL)
    cookie = response.info()['Set-Cookie']
    tree = etree.parse(response, etree.HTMLParser())
    _token = tree.xpath("//input[@name='_token']/@value")[0]
    return cookie, _token

def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        global subjectListDB, semStartDate
        futures = executor.submit(getMMLSCookieToken)

        print('-------------------------------')
        print('|   MMLS Attendance Scraper   |')
        print('-------------------------------')
        print('\nHow do you want to scrape attendance links?:')
        print('1. Retrieve classes via MMLS login and search by date.*')
        print('2. Retrieve classes via MMLS login and search by range of timetable_id.')
        print('\n*/ Unreliable in the first three trimester days and in some cases. ')
        print(' / If no links were caught use the second option instead.          ')
        while True:
            try:
                whatToDo = int(input('\nChoice: '))
                if not 0 < whatToDo < 3: raise ValueError
                break
            except ValueError:
                print('Invalid input.')

        while True:
            userid = input('\nStudent ID: ')
            password = getpass.getpass()
            startTime = time.time()
            cookie, _token = futures.result()
            try:
                response = postURL(mmlsLoginURL, data={'stud_id' : userid, 'stud_pswrd' : password, '_token' : _token}, headers={'Cookie' : cookie})
                print('\nLogged in to MMLS in {:.3f}s.'.format(time.time()-startTime))
                break
            except error.HTTPError as err:
                if err.code == 500: print('Wrong student ID or password.')

        startTime = time.time()
        tree = etree.parse(response, etree.HTMLParser())
        names = [name.split(' - ') for name in tree.xpath("//div[@class='list-group ' and @style='margin-top:-15px']/span/a[1]/text()")]
        links = [link[24:].split(':') for link in tree.xpath("//div[@class='list-group ' and @style='margin-top:-15px']/span/a[1]/@href")]
        for subjectNo, link in enumerate(links):
            subjectListDB.append({
                'subject_code' : names[subjectNo][0], #Eg. ECE2056
                'subject_name' : names[subjectNo][1], #Eg. DATA COMM AND NEWORK
                'subject_id' : links[subjectNo][0], #Eg. 332
                'coordinator_id' : links[subjectNo][1], #Eg. 1585369691
                'classes' : [] #List of classes in dict, with its class code and select attribute.
            })
        print('Parsed registered subjects in {:.3f}s.'.format(time.time()-startTime))

        startTime = time.time()
        futures = [executor.submit(parseClasses, subject, cookie) for subject in subjectListDB]
        for subjectNo, classes in enumerate(futures):
            subjectListDB[subjectNo]['classes'] = classes.result()
        print('Parsed classes in subjects in {:.3f}s.'.format(time.time()-startTime))

        if whatToDo == 1:
            startTime = time.time()
            response = postURL(mobileLoginURL, data={'username' : userid, 'password' : password})
            mobileToken = json.loads(response.read())['token']
            print('Logged into MMU Mobile in {:.3f}s.'.format(time.time()-startTime))

            startTime = time.time()
            response = getURL(mobileSubjectListURL, data={'token' : mobileToken})
            JSON = json.loads(response.read())
            semStartDate = dateFromISOFormat(JSON[0]['sem_start_date'])
            print('Parsed trimester start date in {:.3f}s.'.format(time.time()-startTime))

        print('')
        printSubjectList()
        if askYesNo('\nAutofill?'):
            startTime = time.time()
            futures = [[executor.submit(userInClass, userid, class_['class_id']) for class_ in subject['classes']] for subject in subjectListDB]
            for subjectNo, future in enumerate(futures):
                for classNo, inClass in enumerate(future):
                    if inClass.result(): subjectListDB[subjectNo]['classes'][classNo]['selected'] = True
            getURL(mmlsLogoutURL, headers = {'Cookie' : cookie}) #Expires MMLS cookie
            print('Looked up registered classes in {:.3f}s.'.format(time.time()-startTime))

        while True:
            print('')
            printSubjectList()
            if not askYesNo('\nEdit search selection?'): break
            editSubjectList()

        if whatToDo == 1:
            while True:
                try:
                    startDate = input("Search from what date? YYYY-MM-DD: ")
                    startDate = dateFromISOFormat(startDate) if startDate else (datetime.utcnow() + timedelta(hours=8)).date()
                    startDate = semStartDate if (startDate - semStartDate).days < 0 else startDate
                    if (startDate - semStartDate).days < 3:
                        print("WARNING: Date search is extremely unreliable for searching the first three trimester days.")
                        print("         Expect missing attendance links. Quit and use timetable_id range search instead.")
                    endDate = input("Until what date? YYYY-MM-DD: ")
                    endDate = dateFromISOFormat(endDate) if endDate else startDate
                    break
                except (ValueError, IndexError):
                    print('Invalid format/input.\n')
            startTime = time.time()
            while(True):
                startTimetableID = executor.submit(dateToTimetableID, startDate, 1)
                endTimetableID = executor.submit(dateToTimetableID, endDate, -1)
                startTimetableID, endTimetableID = startTimetableID.result(), endTimetableID.result()
                if not startTimetableID: startDate += timedelta(days=1)
                if not endTimetableID: endDate -= timedelta(days=1)
                if startDate > endDate:
                    print('No timetable_id found in range.')
                    futures = False
                    break
                elif startTimetableID and endTimetableID:
                    print('Found the range of timetable_id at {:.3f}s.'.format(time.time()-startTime))
                    print('Searching classes from {} ({}) to {} ({}).'.format(startTimetableID, startDate.isoformat(), endTimetableID, endDate.isoformat()))
                    futures = [executor.submit(getAttendanceTree, startTimetableID+x) for x in range(endTimetableID-startTimetableID+1)]
                    break
        elif whatToDo == 2:
            while True:
                try:
                    startTimetableID = int(input('Define beginning of timetable_id range: '))
                    endTimetableID = int(input('Define end of timetable_id range: '))
                    break
                except ValueError:
                    print('Invalid input.\n')
            startTime = time.time()
            futures = [executor.submit(getAttendanceTree, startTimetableID+x) for x in range(endTimetableID-startTimetableID+1)]

        while futures: #For as long as there are any futures, result of futures are parsed and printed in order it is submitted.
            html_etree = futures.pop(0).result()
            if not html_etree:
                for future in futures: concurrent.futures.Future.cancel(future)
                del futures
                break
            parsedClassID = html_etree.xpath("//input[@name='class_id']/@value")[0]
            for subject in subjectListDB:
                for class_ in subject['classes']:
                    if class_['selected'] and (parsedClassID == class_['class_id']):
                        print("\n[{} {}-{}] {} - {} ({}) ...at {:.3f}s".format(
                            html_etree.xpath("//input[@name='class_date']/@value")[0],
                            html_etree.xpath("//input[@name='starttime']/@value")[0][:-3],
                            html_etree.xpath("//input[@name='endtime']/@value")[0][:-3],
                            subject['subject_code'], subject['subject_name'], class_['class_name'],
                            time.time()-startTime))
                        print(baseAttendanceURL+":{}:{}:{}".format(
                            subject['subject_id'], subject['coordinator_id'],
                            html_etree.xpath("//input[@name='timetable_id']/@value")[0])) #subjectID and coor.ID don't matter for attendance links
                        print(baseAttendanceListURL+":{}:{}:{}:{}:1".format(
                            subject['subject_id'], subject['coordinator_id'],
                            html_etree.xpath("//input[@name='timetable_id']/@value")[0],
                            class_['class_id'])) #Unlike attendance links, the attendance list links requires all IDs to be correct for the respective subject.
        print("\nCompleted timetable_id scraping attempt in {:.3f}s".format(time.time()-startTime))
        input("Press enter to exit...")

if __name__ == '__main__':
    main()
