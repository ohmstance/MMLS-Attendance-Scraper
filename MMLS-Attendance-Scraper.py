from urllib import request, error, parse
from lxml import etree
from datetime import date, timedelta
from sys import exit
import concurrent.futures
import time
import json
import getpass

baseAttendanceURL = 'https://mmls.mmu.edu.my/attendance'
baseAttendanceListURL = 'https://mmls.mmu.edu.my/viewAttendance'
mmumobileTokenURL = 'https://mmumobileapps.mmu.edu.my/api/auth/login2' #username, password. POST
mmumobileStudentTokenURL = 'https://mmumobileapps.mmu.edu.my/api/camsys/student_key' #token GET
mmlsLoginURL = 'https://mmls.mmu.edu.my/checklogin' #stud_id, stud_pswrd, _token. POST
mmlsURL = 'https://mmls.mmu.edu.my/'
mmlsClassListURL = 'https://mmls.mmu.edu.my/studentlist'
subjectListURL = 'https://mmumobileapps.mmu.edu.my/api/mmls/subject' #token. GET
timetableURL = 'https://mmumobileapps.mmu.edu.my/api/camsys/timetable/' #+<student_token>?token=<token>. GET
maxTimetableID = 99999
minTimetableID = 1
workers = 64
RETRIES = 3
subjectListDB = []

#made by munchbit
def getURL(url, data):
    string = ''
    for index, keyval in enumerate(data.items()):
        if index > 0: string += '&'
        string += '?'+keyval[0]+'='+str(keyval[1])
    req = request.urlopen(url+string, data=None)
    return req

def fetchETree(timetableID): #Accepts timetable_id. Downloads and parses attendance HTML of input timetable_id. Returns ElementTree object, but None type if failed.
    for x in range(RETRIES):
        try:
            html = request.urlopen(baseAttendanceURL+':0:0:'+str(timetableID), timeout=30)
            parser = etree.HTMLParser()
            tree = etree.parse(html, parser)
            return tree
        except error.HTTPError as err:
            if err.code == 500:
                return None
            else:
                continue
        except error.URLError:
            continue
    exit("Network error. Try raising number of retries or obtain better network condition.")

def dateToTimetableID(date, option): #Option: 1 for first occurence, -1 for last occurence; Binary search algorithm; Returns None if there are no classes on that date.
    upperbound = maxTimetableID
    lowerbound = minTimetableID
    while(True):
        currTimetableID = (upperbound+lowerbound)//2
        html_etree = fetchETree(currTimetableID)
        if html_etree is None:
            upperbound = currTimetableID - 1
            continue
        currDate = date.fromisoformat(html_etree.xpath("//input[@name='class_date']")[0].get('value'))
        if (date - currDate).days > 0:
            lowerbound = currTimetableID + 1
        elif (date - currDate).days < 0:
            upperbound = currTimetableID - 1
        elif (date - currDate).days == 0 and option == 1:
            html_etree = fetchETree(currTimetableID - 1)
            if date.fromisoformat(html_etree.xpath("//input[@name='class_date']")[0].get('value')) != currDate:
                return currTimetableID
            upperbound = currTimetableID - 1
        elif (date - currDate).days == 0 and option == -1:
            html_etree = fetchETree(currTimetableID + 1)
            if html_etree is None:
                return currTimetableID
            if date.fromisoformat(html_etree.xpath("//input[@name='class_date']")[0].get('value')) != currDate:
                return currTimetableID
            lowerbound = currTimetableID + 1
        if upperbound < lowerbound:
            return None

def askYesNo(question): #Accepts string -- preferably a question. Returns boolean result where y: True and n: False.
    while True:
        decision = input("{} (y/n): ".format(question))
        if (decision.lower() == 'y'): return True
        elif (decision.lower() == 'n'): return False
        else:
            print("Invalid input.")
            continue

def printSubjectList():
    for index, subject in enumerate(subjectListDB):
        print("{}. {} - {}".format(index+1, subject['subject_code'], subject['subject_name']))
        for className in subject['classes'].keys():
            print('   [{}] {}'.format('X' if className in subject['reg_classes'] else ' ', className))

def editSubjectList():
    try:
        subject = int(input('Select which subject?: '))
        section = input('Toggle which class(es)?: ').split(' ')
        for choice in section:
            for index, className in enumerate(subjectListDB[subject-1]['classes'].keys()):
                if int(choice)-1 == index:
                    if className in subjectListDB[subject-1]['reg_classes']: subjectListDB[subject-1]['reg_classes'].remove(className)
                    else: subjectListDB[subject-1]['reg_classes'].add(className)
    except:
        print('Invalid input.')

def userInClass(student_id, cookie, subject_id, coordinator_id, classID): #returns boolean
    perSubjectClassListURL = mmlsClassListURL + ':' + subject_id + ':' + coordinator_id + ':'
    req = request.Request(perSubjectClassListURL+classID, data=None, headers={'Cookie': cookie}, method='GET')
    response = request.urlopen(req)
    tree = etree.parse(response, etree.HTMLParser())
    for parsedStudentID in tree.xpath("//table/tbody/tr/td/text()"):
        if parsedStudentID == student_id:
            return True
    return False

def main():
    global subjectListDB
    username = input('Student ID: ')
    password = getpass.getpass()

    startTime = time.time()

    data = parse.urlencode({'username': username, 'password': password})
    data = data.encode('utf-8')
    req = request.Request(mmumobileTokenURL, data=data, headers={}, method='POST')
    response = request.urlopen(req)
    mmumobileToken = json.loads(response.read())['token']

    print('\nObtained mmumobile token at {}s.'.format(time.time()-startTime))

    response = getURL(subjectListURL, data={'token': mmumobileToken})
    subjectListJSON = json.loads(response.read()) #subjectListJSON is list[dict{}]
    for subject in subjectListJSON: #Makes initial subject list without classes
        subjectListDB.append({
            'subject_code' : subject['code'], #Eg. ECE2056
            'subject_name' : subject['subject_name'], #Eg. DATA COMM AND NEWORK
            'subject_id' : subject['subject_id'], #Eg. 332
            'coordinator_id' : subject['coordinator_id'], #Eg. 1585369691
            'reg_classes' : set(), #Registered classes
            'classes' : {} #Classes found in class list
        })

    print('Parsed registered subject(s) at {}s.'.format(time.time()-startTime))

    response = request.urlopen(mmlsURL)
    cookie = response.info()['Set-Cookie']
    tree = etree.parse(response, etree.HTMLParser())
    _token = tree.xpath("//input[@name='_token']")[0].get('value')

    data = parse.urlencode({
        'stud_id' : username,
        'stud_pswrd' : password,
        '_token' : _token
    })
    data = data.encode('utf-8')
    req = request.Request(mmlsLoginURL, data=data, headers={'Cookie': cookie}, method='POST')
    response = request.urlopen(req)

    print('Logged in to MMLS at {}s.'.format(time.time()-startTime))

    for index, subject in enumerate(subjectListDB):
        perSubjectClassListURL = mmlsClassListURL + ':' + subject['subject_id'] + ':' + subject['coordinator_id'] + ':0'
        req = request.Request(perSubjectClassListURL, data=None, headers={'Cookie': cookie}, method='GET')
        response = request.urlopen(req)
        tree = etree.parse(response, etree.HTMLParser())
        classIDs = [classID.get('value') for classID in tree.xpath("//select[@id='select_class']/*[not(self::option[@value='0'])]")]
        classNames = tree.xpath("//select[@id='select_class']/*[not(self::option[@value='0'])]/text()")
        subjectListDB[index]['classes'] = dict(zip(classNames, classIDs))

    print('Parsed class(es) in subject(s) at {}s.\n'.format(time.time()-startTime))

    printSubjectList()
    if askYesNo('\nAutomatically fill registered classes?'):
        startTime = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [None] * len(subjectListDB)
            for index, subject in enumerate(subjectListDB):
                futures[index] = {className: executor.submit(userInClass, username, cookie, subject['subject_id'], subject['coordinator_id'], classID) for className, classID in subject['classes'].items()}
            for index, future in enumerate(futures):
                for className, classIDFuture in future.items():
                    if classIDFuture.result():
                        subjectListDB[index]['reg_classes'].add(className)
        print('Registered classes lookup took {}s'.format(time.time()-startTime))
    else:
        print('Manual search selection.')
        editSubjectList()

    while True:
        print('')
        printSubjectList()
        if not askYesNo('\nEdit search selection?'): break
        editSubjectList()

    startDate = date.fromisoformat(input("Search from what date? YYYY-MM-DD: "))
    endDate = date.fromisoformat(input("Until what date? YYYY-MM-DD: "))
    startTime = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        while(True):
            startTimetableID = executor.submit(dateToTimetableID, startDate, 1)
            endTimetableID = executor.submit(dateToTimetableID, endDate, -1)
            startTimetableID = startTimetableID.result()
            endTimetableID = endTimetableID.result()
            if startTimetableID is None:
                startDate += timedelta(days=1)
            if endTimetableID is None:
                endDate -= timedelta(days=1)
            if startDate > endDate:
                print('No classes found in range. It is probably yet to be generated.')
                futures = [] #No jobs assigned.
                break
            if startTimetableID is None or endTimetableID is None:
                continue
            print('Found the range of timetable_id at {}s'.format(time.time()-startTime))
            if startDate == endDate: print('Searching classes in {}.'.format(startDate.isoformat()))
            else: print('Searching classes from {} to {}.'.format(startDate.isoformat(), endDate.isoformat()))
            futures = [executor.submit(fetchETree, startTimetableID+x) for x in range(endTimetableID-startTimetableID+1)]
            break

        while len(futures) > 0: #For as long as there are any futures, result of futures are parsed in order it is submitted.
            html_etree = futures[0].result()
            if html_etree is None:
                for future in futures: concurrent.futures.Future.cancel(future)
                del futures
                break
            parsedClassID = html_etree.xpath("//input[@name='class_id']")[0].get('value')
            for subject in subjectListDB:
                for className, classID in subject['classes'].items():
                    if className in subject['reg_classes']:
                        if (parsedClassID == classID):
                            print("\n{} - {:20} ({}): {} {}-{}".format(
                                subject['subject_code'],
                                subject['subject_name'],
                                className,
                                html_etree.xpath("//input[@name='class_date']")[0].get('value'),
                                html_etree.xpath("//input[@name='starttime']")[0].get('value'),
                                html_etree.xpath("//input[@name='endtime']")[0].get('value')))
                            print(baseAttendanceURL+":{}:{}:{}".format(
                                subject['subject_id'],
                                subject['coordinator_id'],
                                html_etree.xpath("//input[@name='timetable_id']")[0].get('value'))) #Apparently subjectID and coordinatorID doesn't matter for attendance links
                            print(baseAttendanceListURL+":{}:{}:{}:{}:1".format(
                                subject['subject_id'],
                                subject['coordinator_id'],
                                html_etree.xpath("//input[@name='timetable_id']")[0].get('value'),
                                classID)) #Unlike the attendance link, the attendance list link requires all IDs to be correct for the respective subject.
                            break
            del futures[0]

    print("\nCompleted in {}s".format(time.time()-startTime))
    input("Press enter to exit...")

if __name__ == '__main__':
    main()
