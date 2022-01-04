> :warning: **As MMLS is deprecated in favour of MMLS2, the vulnerability has been fixed. This repo is archived.**

# MMLS-Attendance-Scraper
This is a Python script used to scrape MMU attendance by iterating through timetable IDs and returning those with the same class ID. By using this script, you are aware that obtaining attendance in absence is wrong, and that the act might lead to disciplinary action. The author shall not be liable for any direct, indirect, incidental, special, exemplary, or consequential damages however caused arising in any way out of the use of this software. You must use this software for educational purposes only.

An MMLS attendance link is in the format of:  
https://mmls.mmu.edu.my/attendance:[subject_id]:[coordinator_id]:[timetable_id]

Although the subject ID and the coordinator ID is obtainable via MMLS in its HTML code, the timetable ID however isn't easily obtainable. Based on previous attendance links given out by lecturers, the timetable ID consistently increases with time. Skimming through attendance links reveals that each class ID is unique to a class throughout MMU; therefore it is theoretically possible to iterate through all timetable IDs to obtain attendance links for a particular class. This Python script automates that.

### Dependencies
- Python 3.9 (Tested)
- aiohttp (pip module)
- lxml (pip module)

### Glossary
- Subject ID: A variable digit numerical value no more than five digits. It corresponds to a subject.
- Coordinator ID: A ten digit numerical value corresponding to the coordinator tied to a subject.
- Class ID: A five digit numerical value tied to a class within a subject.
- Timetable ID: A multi-digit number tied to a single session of a class throughout the academic year. It is pregenerated two days ahead of time by MMLS.

### Addendum
https://github.com/ToraNova/sleep-in  
ToraNova created an aptly named 'sleep-in' back in the end of 2018 probably as an assignment. Well, until now, this vulnerability still isn't fixed. That was written in Java and differs to this in that the program bruteforces sign-ins through attendance links in range of timetable_id. However, as it is not concurrent, it takes a while.
