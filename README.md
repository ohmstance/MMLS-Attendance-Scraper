<sub><sub>MMU pls fix</sub></sub>
# MMLS-Attendance-Scraper
This is a Python script able to scrape MMU attendance by iterating through timetable IDs and returning those with the same class ID. By using this script, you are aware that obtaining attendance in absence is wrong, and that the act might lead to disciplinary action. The author shall not be liable for any direct, indirect, incidental, special, exemplary, or consequential damages however caused arising in any way out of the use of this software. You must use this software for educational purposes only.

A MMLS attendance link is in the format of:  
https://mmls.mmu.edu.my/attendance:<subjectID\>:<coordinatorID\>:<timetableID\>

Although the subject ID and the coordinator ID is obtainable via MMLS in its HTML code, the timetable ID however isn't easily obtainable. Based on previous attendance links given out by lecturers, the timetable ID consistently increases with time. Skimming through attendance links reveals that each class ID is unique to a class throughout MMU; therefore it is theoretically possible to iterate through all timetable IDs to obtain attendance links for a particular class. This Python script automates that.

### Dependencies
- Python 3.2
- lxml (pip module)

### Glossary
- Subject ID: A variable digit numerical value no more than five digits. It corresponds to a subject.
- Coordinator ID: A ten digit numerical value corresponding to the coordinator tied to a subject.
- Class ID: A five digit numerical value tied a classes within a subject.
- Timetable ID: A variable digit numerical value tied to a single session of a class throughout the academic year. It is pregenerated -- probably -- one or two days ahead of time by MMLS.

### Future work
- [x] Terminate HTML fetching upon encountering ungenerated attendance link. (Error 500)
- [x] Scrape attendance link by date instead of a range of timetable ID.
- [x] Use lxml instead of BeautifulSoup for performance.
- [x] Automatically retrieve list of subjects with corresponding IDs via MMLS login.
- [ ] Check how many students have attended in each class session
- [ ] Allow manual input of parameters
- [ ] Implement paralellized linear search algorithm for timetable_id
- [ ] Paralellize binary search algorithm further

### Addendum
https://github.com/ToraNova/sleep-in  
It appears I am not the first. ToraNova created an aptly named 'sleep-in' back in the end of 2018 probably as an assignment. Well, until now, this weakness still isn't fixed. His' is written in Java and differs to mine in that the software bruteforces sign-ins through attendance links one-by-one in a specified range of timetable_id. Though, it's slow as it's not multithreaded.
