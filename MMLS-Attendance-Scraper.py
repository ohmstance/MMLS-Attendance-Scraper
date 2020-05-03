import concurrent.futures
import time
from urllib import request, error
from lxml import etree
from datetime import date, timedelta
from sys import exit

baseAttendanceURL = 'https://mmls.mmu.edu.my/attendance'
baseAttendanceListURL = 'https://mmls.mmu.edu.my/viewAttendance'
maxTimetableID = 99999
minTimetableID = 1
RETRIES = 3

#made by munchbit
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

def main():
    counter = 1
    subjectID, coordinatorID, classID = [], [], []
    while(True): #Good enough
        subjectID.append(input("Class #{}'s Subject ID: ".format(counter)))
        coordinatorID.append(input("Class #{}'s Coordinator ID: ".format(counter)))
        classID.append(input("Class #{}'s Class ID: ".format(counter)))
        if (askYesNo("Enter more classes?") == False): break
        counter += 1

    print("\n{:10}{:12}{:16}{:12}".format('', "Subject ID", "Coordinator ID", "Class ID"))
    for index in range(len(subjectID)):
        print("{:10}{:12}{:16}{:12}".format("Class #{}".format(index+1), subjectID[index], coordinatorID[index], classID[index]))

    startDate = date.fromisoformat(input("\nSearch from what date? YYYY-MM-DD: "))
    endDate = date.fromisoformat(input("Until what date? YYYY-MM-DD: "))
    # workers = int(input("Number of CPU threads for parsing?: "))
    workers = 64

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
                print("No classes exist in range.")
            if startTimetableID is None or endTimetableID is None:
                continue
            print("\nNow searching from {} to {} at {}s.".format(startDate.isoformat(), endDate.isoformat(), time.time()-startTime))
            break

        futures = [executor.submit(fetchETree, startTimetableID+x) for x in range(endTimetableID-startTimetableID+1)]
        while len(futures) > 0: #For as long as there are any futures, result of futures are parsed in order it is submitted.
            html_etree = futures[0].result()
            if html_etree is None:
                for future in futures: concurrent.futures.Future.cancel(future)
                del futures
                break
            parsedClassID = html_etree.xpath("//input[@name='class_id']")[0].get('value')
            for index, ID in enumerate(classID):
                if (parsedClassID == ID):
                    print("\nClass {}: {} from {} to {} fetched in {}s".format(
                        ID,
                        html_etree.xpath("//input[@name='class_date']")[0].get('value'),
                        html_etree.xpath("//input[@name='starttime']")[0].get('value'),
                        html_etree.xpath("//input[@name='endtime']")[0].get('value'),
                        time.time()-startTime))
                    print(baseAttendanceURL+":{}:{}:{}".format(
                        subjectID[index],
                        coordinatorID[index],
                        html_etree.xpath("//input[@name='timetable_id']")[0].get('value'))) #Apparently subjectID and coordinatorID doesn't matter for attendance links
                    print(baseAttendanceListURL+":{}:{}:{}:{}:1".format(
                        subjectID[index],
                        coordinatorID[index],
                        html_etree.xpath("//input[@name='timetable_id']")[0].get('value'),
                        ID)) #Unlike the attendance link, the attendance list link requires all IDs to be correct for the respective subject.
                    break
            del futures[0]

    print("\nCompleted in {}s".format(time.time()-startTime))
    input("Press enter to exit...")

if __name__ == '__main__':
    main()
