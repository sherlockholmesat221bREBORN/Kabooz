\  
# DEV LOGS  
  
This file records logs written by the developer for future reference (self or collaborators).  
This is NOT user documentation; it exists purely for developer reference.  
Entries may be incomplete, outdated, or inaccurate.  
  
## Format  
1. An entry should be made every session.  
   A session usually corresponds to a single day.  
2. All dates must be in UTC. Mentioning time is discouraged; if used, it must also be in UTC.  
3. This file should be updated in the last commit of a session, and should be omitted in **Commits Made** list.  
4. A single session entry must follow this format:  
  
    ### YYYY-MM-DD  
    **Author**: [Your name / alias]  
    **Commits Made**: commit list or range  
  
    - Use bullet points.  
    - Use explicit sub-headings (####) such as:  
      - TODO  
      - DONE  
      - NOTES  
      - BUGS  
      - IDEAS  
  
---  
  

### 2026-03-17
**Author**: [TchaikovskyCannonsAPI]  
**Commits Made**: 6a88b36..c477381
#### DONE
- [x] Lets leave it, cuz it would be soo long
#### BUGS
- When downloading MP3, the template adds this in the folder name `Billie Jean [FLAC 16bit 44.1kHz] [2026]`
- When running TUI curses, and clicking on Mozart's Don Giovanni album by Carlo ... remastered 2016, it returns the error ` Overture  [24bit/96kHz] Philharmonia Orchestra  —  Mozart : Don Giovanni (2016 Remastered) █░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  0:18 / 6:18 ⏸ PAUSED  space=pause  n=next  p=prev  r=radio  [/]=vol 100%  ←/→=±10s  q=quit Loading: Mozart : Don Giovanni (2016 Remastered)… [DEV] GET /album/get params=['album_id', 'limit', 'offset'] → fetching…                                                                                                                          [DEV] GET /album/get params=['album_id', 'limit', 'offset'] → fetching…                [DEV] GET /album/get → HTTP 200 (cached) ✗ Load error: 'TrackSummary' object has no attribute 'performer' [DEV] GET /album/get → HTTP 200 (cached)`

