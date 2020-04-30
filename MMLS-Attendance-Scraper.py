from urllib.request import urlopen
from urllib.error import HTTPError, URLError
from bs4 import BeautifulSoup
from sys import exit as exit
from datetime import date
import concurrent.futures
import time
RETRIES = 3

#made by munchbit
def fetchHTML(timetableID): #Accepts timetable_id. Downloads attendance HTML bearing the timetable_id. If successful, returns BeautifulSoup HTML object, but returns None type otherwise.
    for x in range(RETRIES):
        try:
            html = BeautifulSoup(urlopen('https://mmls.mmu.edu.my/attendance:0:0:'+str(timetableID), timeout=30), 'html.parser') #Apparently subjectID and coordinatorID doesn't matter for attendance links
            return html
        except HTTPError as error:
            if error.code == 500:
                return None
            else:
                continue
        except URLError:
            continue
    exit("Network error. Try raising number of retries or obtain better network condition.")

def dateToTimetableID(date, option): #Accepts date and option to choose either returning the first occurence or the last. Returns corresponding timetable_id satisfying the option selected.
    MIN_TimetableID = 1
    MAX_TimetableID = 99999
    currTimetableID = -(-(MAX_TimetableID+MIN_TimetableID)//2)
    step = -(-(MAX_TimetableID+MIN_TimetableID)//2)
    previousAction = 0 #0: No action; -1: Stepped backwards; 1:  Stepped forwards.
    while(True):
        if currTimetableID < MIN_TimetableID:
            step -= MIN_TimetableID - currTimetableID
            currTimetableID = MIN_TimetableID
        if currTimetableID > MAX_TimetableID:
            step -= currTimetableID - MAX_TimetableID
            currTimetableID = MAX_TimetableID

        html = fetchHTML(currTimetableID)
        if html is None:
            if previousAction != -1:
                if step == 1: return None
                step = -(-step)//2
            currTimetableID -= step
            previousAction = -1
            continue

        currDate = date.fromisoformat(html.find('input', id="class_date")['value'])
        if (date-currDate).days < 0: #Desired date falls behind current date
            if previousAction != -1:
                if step == 1: return None #Terminates seek when timetable_id for the date does not exist
                step = -(-step)//2
            currTimetableID -= step
            previousAction = -1
        elif (date-currDate).days > 0: #Desired date is ahead of current date
            if previousAction != 1:
                if step == 1: return None
                step = -(-step)//2
            currTimetableID += step
            previousAction = 1
        elif option == "first":
            html = fetchHTML(currTimetableID-1)
            if html is None: return currTimetableID
            elif date != date.fromisoformat(fetchHTML(currTimetableID-1).find('input', id="class_date")['value']): return currTimetableID
            elif previousAction != -1: step = -(-step)//2
            currTimetableID -= step
            previousAction = -1
        elif option == "last":
            html = fetchHTML(currTimetableID+1)
            if html is None: return currTimetableID #handle error 500 in next timetable_id
            elif date != date.fromisoformat(html.find('input', id="class_date")['value']): return currTimetableID #if next timetable_id's day is different
            elif previousAction != 1: step = -(-step)//2 #continues here if next timetable_id's day is the same
            currTimetableID += step
            previousAction = 1

def askYesNo(question): #Accepts string -- preferably a question. Ask the user if yes, or no. Returns boolean result where y: True and n: False.
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
        startTimetableID = executor.submit(dateToTimetableID, startDate, "first")
        endTimetableID = executor.submit(dateToTimetableID, endDate, "last")
        startTimetableID = startTimetableID.result()
        endTimetableID = endTimetableID.result()
        print("\nRange of timetable_id found. {}s elapsed.".format(time.time()-startTime))
        print("Scraping started.")

        futures = [executor.submit(fetchHTML, startTimetableID+x) for x in range(endTimetableID-startTimetableID+1)]
        while len(futures) > 0: #For as long as there are any futures, result of futures are parsed in order it is submitted. Once done, the queue (list of futures) is popped and the elements are shifted forward. All elements are accessible sequentially in the first element as queue is popped.
            if futures[0].result() is None:
                for future in futures: concurrent.futures.Future.cancel(future)
                del futures
                break
            parsedClassID = futures[0].result().find('input', id="class_id")['value']
            for ID in classID:
                if (parsedClassID == ID):
                    print("\nClass {}: {} from {} to {} fetched in {}s".format(ID,
                        futures[0].result().find('input', id="class_date")['value'],
                        futures[0].result().find('input', id="starttime")['value'],
                        futures[0].result().find('input', id="endtime")['value'],
                        time.time()-startTime))
                    print("https://mmls.mmu.edu.my/attendance:{}:{}:{}".format(subjectID[classID.index(ID)], coordinatorID[classID.index(ID)], futures[0].result().find('input', id="timetable_id")['value'])) #Returns the attendance link faithful to the real generated link that includes the correct subject id and coordinator id although it doesn't matter in practice -- the attendance system does not check for both of them whether they are for the right subject and coordinator.
                    print("https://mmls.mmu.edu.my/viewAttendance:{}:{}:{}:{}:1".format(subjectID[classID.index(ID)], coordinatorID[classID.index(ID)], futures[0].result().find('input', id="timetable_id")['value'], ID)) #Unlike the attendance link, the attendance list link requires all IDs to be correct for the respective subject.
                    break
            del futures[0]

    print("\nCompleted in {}s".format(time.time()-startTime))
    input("Press enter to exit...")

if __name__ == '__main__':
    main()
