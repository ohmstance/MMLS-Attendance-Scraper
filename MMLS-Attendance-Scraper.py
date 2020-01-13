from urllib.request import urlopen
from urllib.error import HTTPError, URLError
from bs4 import BeautifulSoup
import concurrent.futures
import time
#made by munchbit
def fetchHTML(timetableID):
    try:
        html = BeautifulSoup(urlopen(fetchLink+str(timetableID)), 'html.parser')
        return html
    except(HTTPError, URLError):
        return None

def askYesNo(question):
    decision = input("{} (y/n): ".format(question))
    while True:
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
workers = int(input("How many CPU threads?: "))
fetchLink = "https://mmls.mmu.edu.my/attendance:{}:{}:".format(subjectID[0], coordinatorID[0]) #Subject id and coordinator id does not matter when searching for timetable id
startTime = time.time()

with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
    futureFetch = [executor.submit(fetchHTML, startTimetableID+x) for x in range(endTimetableID-startTimetableID+1)]
    for future in concurrent.futures.as_completed(futureFetch):
        if future.result() is None:
            del future
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
                break
        del future

print("\nCompleted in {}s".format(time.time()-startTime))
input("Press enter to exit...")
