# MMLS-Attendance-Scraper
A Python script used to scrape MMU attendance by iterating through timetable IDs and shortlisting those with the same subject ID.

A MMLS attendance link is in the format of:  
https://mmls.mmu.edu.my/attendance:<subjectID\>:<coordinatorID\>:<timetableID\>

Although the subject ID and the coordinator ID is obtainable via MMLS by searching through its HTML code, the timetable ID however isn't easily obtainable. Based on previous attendance links given out by lecturers, the timetable ID consistently increases with time. Skimming through the HTML code reveals that each timetable ID is unique to a class throughout MMU for a particular semester; therefore it is theoretically possible to iterate through all timetable IDs to obtain attendance links for a particular class. This Python script automates that.

### Dependencies
- Python 3.2
- lxml (pip module)

### Glossary
- Subject ID: A variable digit numerical value no more than five digits. It corresponds to a subject.
- Coordinator ID: A ten digit numerical value corresponding to the coordinator tied to a subject.
- Class ID: A five digit numerical value tied a classes within a subject.
- Timetable ID: A variable digit numerical value tied to a single session of a class throughout the semester. It is pregenerated one or two days ahead of time and does not require manual QR generation from a lecturer for it to exist.

### Future work
- Automatically retrieve list of subjects with corresponding IDs via MMLS login.
- ~~Terminate HTML fetching upon encountering ungenerated attendance link. (Error 500)~~
- ~~Scrape attendance link by date instead of a range of timetable ID.~~
- ~~Use lxml instead of BeautifulSoup for performance.~~
