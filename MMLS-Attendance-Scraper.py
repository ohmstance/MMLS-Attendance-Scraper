from urllib.request import urlopen
from urllib.error import HTTPError, URLError
from bs4 import BeautifulSoup
from sys import exit as exit
import concurrent.futures
import time
RETRIES = 3

#made by munchbit
def fetchHTML(timetableID):
    for x in range(RETRIES):
        try:
            html = BeautifulSoup(urlopen('https://mmls.mmu.edu.my/attendance:0:0:'+str(timetableID)), 'html.parser') #Apparently subjectID and coordinatorID doesn't matter for attendance links
            return html
        except(HTTPError) as error:
            if error.code == 500:
                return None
        except(HTTPError, URLError):
            continue
    exit("Network error. Try raising number of retries or obtain better network condition.")


def askYesNo(question):
    while True:
        decision = input("{} (y/n): ".format(question))
        if (decision.lower() == 'y'): return True
        elif (decision.lower() == 'n'): return False
        else:
            print("Invalid input.")
            continue

counter = 1
subjectID, coordinatorID, classID = [], [], []
while(True): #Good enough
    subjectID.append(input("Class #{}'s Subject ID: ".format(counter)))
    coordinatorID.append(input("Class #{}'s Coordinator ID: ".format(counter)))
    classID.append(input("Class #{}'s Class ID: ".format(counter)))
    if (askYesNo("Enter more classes?") == False): break
    counter += 1

print("\n{:10}{:12}{:16}{:12}".format('', "Subject ID", "Coordinator ID", "Class ID"))
for classNum in range(counter):
    print("{:10}{:12}{:16}{:12}".format("Class #{}".format(classNum+1), subjectID[classNum], coordinatorID[classNum], classID[classNum]))

startTimetableID = int(input("\nStart timetable_id?: "))
endTimetableID = int(input("End timetable_id?: "))
workers = int(input("How many links to parse at a time? [Recommended: 100]: "))

startTime = time.time()

queue = endTimetableID - startTimetableID + 1 #Because startTimetableID is included, add one to total queues.
currTimetableID = startTimetableID
foundUngenerated = False #Assumes HTTPError or URLError is the result of ungenerated attendance links.
with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
    while not (queue <= 0 or foundUngenerated):
        queue -= workers
        task = workers
        if queue < 0: task += queue #If queued past endTimetableID, remove excess scheduled task.
        futureFetch = [executor.submit(fetchHTML, currTimetableID+x) for x in range(task)]

        for future in concurrent.futures.as_completed(futureFetch):
            if future.result() is None:
                foundUngenerated = True
                continue

            parsedClassID = future.result().find('input', id="class_id")['value']
            for ID in classID:
                if (parsedClassID == ID):
                    print("\nClass {}: {} from {} to {} fetched in {}s".format(ID,
                        future.result().find('input', id="class_date")['value'],
                        future.result().find('input', id="starttime")['value'],
                        future.result().find('input', id="endtime")['value'],
                        time.time()-startTime))
                    print("https://mmls.mmu.edu.my/attendance:{}:{}:{}".format(subjectID[classID.index(ID)], coordinatorID[classID.index(ID)], future.result().find('input', id="timetable_id")['value'])) #Returns the attendance link faithful to the real generated link that includes the correct subject id and coordinator id although it doesn't matter in practice -- the attendance system does not check for both of them whether they are for the right subject and coordinator.
                    print("https://mmls.mmu.edu.my/viewAttendance:{}:{}:{}:{}:1".format(subjectID[classID.index(ID)], coordinatorID[classID.index(ID)], future.result().find('input', id="timetable_id")['value'], ID)) #Unlike the attendance link, the attendance list link requires all IDs to be correct for the respective subject.
                    break

        currTimetableID += task
        del futureFetch

print("") #New line why not
if foundUngenerated: print("Scraping aborted in interval {} to {}.".format(currTimetableID-task, currTimetableID-task+workers))
print("Completed in {}s".format(time.time()-startTime))
input("Press enter to exit...")
