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
mmumobileTokenURL = 'https://mmumobileapps.mmu.edu.my/api/auth/login2?' #username, password. POST
mmumobileStudentTokenURL = 'https://mmumobileapps.mmu.edu.my/api/camsys/student_key?' #token GET
mmlsLoginURL = 'https://mmls.mmu.edu.my/checklogin?' #stud_id, stud_pswrd, _token. POST
mmlsURL = 'https://mmls.mmu.edu.my/'
mmlsClassListURL = 'https://mmls.mmu.edu.my/studentlist'
subjectListURL = 'https://mmumobileapps.mmu.edu.my/api/mmls/subject?' #token. GET
timetableURL = 'https://mmumobileapps.mmu.edu.my/api/camsys/timetable/' #+<student_token>?token=<token>. GET
maxTimetableID = 99999
minTimetableID = 1
workers = 64
RETRIES = 3
subjectListDB = []

def getURL(url, *, data={}, headers={}):
    string = parse.urlencode(data)
    req = request.Request(url+string, data=None, headers=headers, method='GET')
    response = request.urlopen(req)
    return response

def fetchETree(timetableID): #Accepts timetable_id. Downloads and parses attendance HTML of input timetable_id. Returns ElementTree object, but None type if failed.
    for x in range(RETRIES):
        try:
            html = request.urlopen(baseAttendanceURL+':0:0:'+str(timetableID), timeout=30)
            tree = etree.parse(html, etree.HTMLParser())
            return tree
        except error.HTTPError as err:
            if err.code == 500: return None
        except error.URLError:
            pass
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
        for class_ in subject['classes']:
            print('   [{}] {}'.format('X' if class_['selected'] else ' ', class_['class_name']))

def editSubjectList():
    try:
        subject = int(input('Select which subject?: '))-1
        classes = [int(class_)-1 for class_ in input("Toggle which classes?: ").split(' ')]
        for class_ in classes:
            subjectListDB[subject]['classes'][class_]['selected'] = not subjectListDB[subject]['classes'][class_]['selected']
    except (ValueError, IndexError):
        print('Invalid input.')

def parseClasses(subject, cookie):
        subjectClassListURL = mmlsClassListURL + ':' + subject['subject_id'] + ':' + subject['coordinator_id'] + ':0'
        response = getURL(subjectClassListURL, headers={'Cookie': cookie})
        tree = etree.parse(response, etree.HTMLParser())
        classIDs = [classID.get('value') for classID in tree.xpath("//select[@id='select_class']/*[not(self::option[@value='0'])]")]
        classNames = tree.xpath("//select[@id='select_class']/*[not(self::option[@value='0'])]/text()")
        classList = [{'class_name' : class_[0], 'class_id' : class_[1], 'selected' : False} for class_ in zip(classNames, classIDs)]
        return classList

def userInClass(student_id, class_id, subject, cookie): #returns boolean
    url = '{}:{}:{}:{}'.format(mmlsClassListURL, subject['subject_id'], subject['coordinator_id'], class_id) #mmlsClassListURL+':'+subject['subject_id']+':'+subject['coordinator_id']+':'
    response = getURL(url, headers={'Cookie': cookie})
    tree = etree.parse(response, etree.HTMLParser())
    return True if tree.xpath("//table/tbody/tr/td[text()='{}']".format(student_id)) else False

def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        global subjectListDB
        while True:
            userid = input('\nStudent ID: ')
            password = getpass.getpass()

            startTime = time.time()
            data = parse.urlencode({'username': userid, 'password': password})
            data = data.encode('utf-8')
            req = request.Request(mmumobileTokenURL, data=data, headers={}, method='POST')
            try:
                response = request.urlopen(req)
            except error.HTTPError as err:
                if err.code == 422:
                    print('Invalid student ID or password.')
                    continue
            break
        mmumobileToken = json.loads(response.read())['token']

        print('\nObtained mmumobile token at {:.3f}s.'.format(time.time()-startTime))

        response = getURL(subjectListURL, data={'token': mmumobileToken})
        subjectListJSON = json.loads(response.read()) #subjectListJSON is list[dict{}]
        for subject in subjectListJSON: #Makes initial subject list without classes
            subjectListDB.append({
                'subject_code' : subject['code'], #Eg. ECE2056
                'subject_name' : subject['subject_name'], #Eg. DATA COMM AND NEWORK
                'subject_id' : subject['subject_id'], #Eg. 332
                'coordinator_id' : subject['coordinator_id'], #Eg. 1585369691
                'classes' : [] #List of classes, with its class code and select attribute.
            })

        print('Parsed registered subject(s) at {:.3f}s.'.format(time.time()-startTime))

        response = request.urlopen(mmlsURL)
        cookie = response.info()['Set-Cookie']
        tree = etree.parse(response, etree.HTMLParser())
        _token = tree.xpath("//input[@name='_token']")[0].get('value')

        print('Obtained _token string at {:.3f}s'.format(time.time()-startTime))

        data = parse.urlencode({
            'stud_id' : userid,
            'stud_pswrd' : password,
            '_token' : _token
        })
        data = data.encode('utf-8')
        req = request.Request(mmlsLoginURL, data=data, headers={'Cookie': cookie}, method='POST')
        response = request.urlopen(req)

        print('Logged in to MMLS at {:.3f}s.'.format(time.time()-startTime))

        futures = [executor.submit(parseClasses, subject, cookie) for subject in subjectListDB]
        for index, classesFuture in enumerate(futures):
            subjectListDB[index]['classes'] = classesFuture.result()

        print('Parsed class(es) in subject(s) at {:.3f}s.\n'.format(time.time()-startTime))

        printSubjectList()
        if askYesNo('\nAutomatically fill registered classes?'):
            startTime = time.time()
            futures = [[executor.submit(userInClass, userid, class_['class_id'], subject, cookie) for class_ in subject['classes']] for subject in subjectListDB]
            for subjectIndex, future in enumerate(futures):
                for classIndex, classExistFuture in enumerate(future):
                    if classExistFuture.result():
                        subjectListDB[subjectIndex]['classes'][classIndex]['selected'] = True
            print('Registered classes lookup took {:.3f}s'.format(time.time()-startTime))
        else:
            print('Manual search selection.')
            editSubjectList()

        while True:
            print('')
            printSubjectList()
            if not askYesNo('\nEdit search selection?'): break
            editSubjectList()

        while True:
            try:
                startDate = date.fromisoformat(input("Search from what date? YYYY-MM-DD: "))
                endDate = date.fromisoformat(input("Until what date? YYYY-MM-DD: "))
                break
            except ValueError:
                print('Invalid format/input.\n')
                continue
        startTime = time.time()
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
                futures = False
                break
            if startTimetableID is None or endTimetableID is None:
                continue
            print('Found the range of timetable_id at {:.3f}s'.format(time.time()-startTime))
            if startDate == endDate:
                print('Searching classes in {}.'.format(startDate.isoformat()))
            else:
                print('Searching classes from {} to {}.'.format(startDate.isoformat(), endDate.isoformat()))
            futures = [executor.submit(fetchETree, startTimetableID+x) for x in range(endTimetableID-startTimetableID+1)]
            break

        while futures: #For as long as there are any futures, result of futures are parsed in order it is submitted.
            html_etree = futures[0].result()
            if html_etree is None:
                for future in futures: concurrent.futures.Future.cancel(future)
                del futures
                break
            parsedClassID = html_etree.xpath("//input[@name='class_id']")[0].get('value')
            for subject in subjectListDB:
                for class_ in subject['classes']:
                    if class_['selected']:
                        if (parsedClassID == class_['class_id']):
                            print("\n{} - {:20} ({}): {} {}-{} (at {:.3f}s)".format(
                                subject['subject_code'],
                                subject['subject_name'],
                                class_['class_name'],
                                html_etree.xpath("//input[@name='class_date']")[0].get('value'),
                                html_etree.xpath("//input[@name='starttime']")[0].get('value')[:-3],
                                html_etree.xpath("//input[@name='endtime']")[0].get('value')[:-3],
                                time.time()-startTime))
                            print(baseAttendanceURL+":{}:{}:{}".format(
                                subject['subject_id'],
                                subject['coordinator_id'],
                                html_etree.xpath("//input[@name='timetable_id']")[0].get('value'))) #Apparently subjectID and coordinatorID doesn't matter for attendance links
                            print(baseAttendanceListURL+":{}:{}:{}:{}:1".format(
                                subject['subject_id'],
                                subject['coordinator_id'],
                                html_etree.xpath("//input[@name='timetable_id']")[0].get('value'),
                                class_['class_id'])) #Unlike the attendance link, the attendance list link requires all IDs to be correct for the respective subject.
                            break
            del futures[0]

        print("\nCompleted timetable ID scraping attempt in {:.3f}s".format(time.time()-startTime))
        input("Press enter to exit...")

if __name__ == '__main__':
    main()
