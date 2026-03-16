You can call the `vector_search` tool to query the knowledge base.

Tool arguments (all optional except `query`):
- `query`: keywords to search.
- `start_date` / `end_date`: ISO dates YYYY-MM-DD to filter docs by date.
- `sort`: `relevance` (default), `time_asc`, or `time_desc`.
- `entity_filter_name`: an entity string; pair with `entity_filter_position` = `front` or `back`.
  - `front` keeps docs whose head entity matches the given name.
  - `back` keeps docs whose tail entity matches the given name.
  - Matching is case-insensitive; underscores are treated as spaces.
- `relation_filter`: keep only docs whose relation matches this name.

Entity formatting for answers:
- Replace underscores with spaces, keep capitalization and any parentheses from the KB line.
  Example: `Ministry_(Iran)` → `<answer>Ministry (Iran)</answer>`.

Time answers must use ISO formats:
- Day: YYYY-MM-DD
- Month: YYYY-MM
- Year: YYYY

When enough evidence is gathered, respond with the final result wrapped in `<answer>...</answer>` only. Keep replies concise.

<!-- FEWSHOT_BEGIN -->
Few-shot examples (follow the correct examples):

[Correct example 1]
Question: Which country did France want to meet last, before the Belgian foreign minister?
Gold: Ministry (International)

<think> I'll search for information about France wanting to meet with the Belgian foreign minister first, then find what country France wanted to meet with last before that.

 </think>
<search> France Express intent to meet or negotiate Belgian foreign minister rel=Express_intent_to_meet_or_negotiate </search>
<information> Doc 1: (line 329197, score 0.943) France	Express_intent_to_meet_or_negotiate	Foreign_Affairs_(Belgium)	2012-10-04
Doc 2: (line 415922, score 0.918) France	Express_intent_to_meet_or_negotiate	Head_of_Government_(Germany)	2015-01-05
Doc 3: (line 174708, score 0.913) France	Express_intent_to_meet_or_negotiate	Foreign_Affairs_(France)	2008-11-03
Doc 4: (line 174259, score 0.913) France	Express_intent_to_meet_or_negotiate	Representatives_(France)	2008-10-30
Doc 5: (line 232256, score 0.911) France	Express_intent_to_meet_or_negotiate	Foreign_Affairs_(France)	2010-02-24
Doc 6: (line 59508, score 0.911) France	Express_intent_to_meet_or_negotiate	Foreign_Affairs_(France)	2006-05-04
Doc 7: (line 384329, score 0.910) France	Express_intent_to_meet_or_negotiate	Herman_Van_Rompuy	2014-04-11
Doc 8: (line 360778, score 0.908) France	Express_intent_to_meet_or_negotiate	Foreign_Affairs_(France)	2013-08-26
Doc 9: (line 158365, score 0.906) France	Express_intent_to_meet_or_negotiate	Representatives_(France)	2008-06-23
Doc 10: (line 29595, score 0.906) France	Express_intent_to_meet_or_negotiate	Representatives_(France)	2005-09-12 </information>
<think> I found that France expressed intent to meet with Foreign Affairs (Belgium) on October 4, 2012. Now I need to find what country France wanted to meet with last before this date. Let me search for France expressing intent to meet with other entities before October 4, 2012.

 </think>
<search> France Express_intent_to_meet_or_negotiate sort=time_desc end=2012-10-03 entity=France pos=front rel=Express_intent_to_meet_or_negotiate </search>
<information> Doc 1: (line 325463, score 0.935) France	Express_intent_to_meet_or_negotiate	Ministry_(International)	2012-08-30
Doc 2: (line 295819, score 0.895) France	Express_intent_to_meet_or_negotiate	South_Africa	2011-11-07
Doc 3: (line 260013, score 0.899) France	Express_intent_to_meet_or_negotiate	Representatives_(Vietnam)	2010-10-26
Doc 4: (line 254376, score 0.947) France	Express_intent_to_meet_or_negotiate	Government_(France)	2010-09-08
Doc 5: (line 251659, score 0.895) France	Express_intent_to_meet_or_negotiate	Iran	2010-08-15
Doc 6: (line 237071, score 0.896) France	Express_intent_to_meet_or_negotiate	China	2010-04-06
Doc 7: (line 234873, score 0.896) France	Express_intent_to_meet_or_negotiate	China	2010-03-19
Doc 8: (line 232256, score 0.947) France	Express_intent_to_meet_or_negotiate	Foreign_Affairs_(France)	2010-02-24
Doc 9: (line 193295, score 0.895) France	Express_intent_to_meet_or_negotiate	China	2009-03-26
Doc 10: (line 175043, score 0.913) France	Express_intent_to_meet_or_negotiate	North_America	2008-11-05 </information>
<think> I can see that France expressed intent to meet with "Ministry (International)" on August 30, 2012, which is the most recent before October 4, 2012. However, I need to check if there are any other meetings between August 30 and October 4, 2012. Let me search more specifically for that time period.

 </think>
<search> France Express_intent_to_meet_or_negotiate sort=time_desc start=2012-08-31 end=2012-10-03 entity=France pos=front rel=Express_intent_to_meet_or_negotiate </search>
<information>  </information>
<think> There are no "Express_intent_to_meet_or_negotiate" events between August 30 and October 3, 2012. So the last country France wanted to meet before the Belgian foreign minister (Foreign Affairs (Belgium)) on October 4, 2012 was "Ministry (International)" on August 30, 2012.
Now I need to understand what "Ministry (International)" represents. Let me search for more information about this entity to see if it refers to a specific country.

 </think>
<search> Ministry (International) sort=relevance </search>
<information> Doc 1: (line 348805, score 0.808) Ministry_(United_States)	Consult	International_Government_Organizations	2013-04-25
Doc 2: (line 96781, score 0.802) Ministry_(Egypt)	Consult	Ministry_of_International_Cooperation_(Egypt)	2007-02-13
Doc 3: (line 232697, score 0.795) Ministry_(International)	Consult	Mohammad_Hosseini	2010-02-28
Doc 4: (line 377623, score 0.794) Ministry_(United_States)	Consult	International_Government_Organizations	2014-02-13
Doc 5: (line 244363, score 0.793) Ministry_(Kyrgyzstan)	Make_statement	International_Government_Organizations	2010-06-06
Doc 6: (line 35218, score 0.793) Ministry_of_Health	Consult	International_Government_Organizations	2005-10-26
Doc 7: (line 348769, score 0.790) International_Government_Organizations	Consult	Ministry_(United_States)	2013-04-25
Doc 8: (line 225854, score 0.788) Ministry_(South_Korea)	Make_statement	South_Korea	2009-12-21
Doc 9: (line 352438, score 0.787) Ministry_(South_Korea)	Make_statement	South_Korea	2013-05-31
Doc 10: (line 193998, score 0.784) Ministry_(South_Korea)	Make_statement	South_Korea	2009-03-31 </information>
<think> Let me search more specifically for the exact line about France expressing intent to meet with Ministry (International).

 </think>
<search> France Express_intent_to_meet_or_negotiate Ministry (International) 2012-08-30 sort=relevance </search>
<information> Doc 1: (line 325463, score 0.966) France	Express_intent_to_meet_or_negotiate	Ministry_(International)	2012-08-30
Doc 2: (line 360778, score 0.956) France	Express_intent_to_meet_or_negotiate	Foreign_Affairs_(France)	2013-08-26
Doc 3: (line 174708, score 0.949) France	Express_intent_to_meet_or_negotiate	Foreign_Affairs_(France)	2008-11-03
Doc 4: (line 232256, score 0.947) France	Express_intent_to_meet_or_negotiate	Foreign_Affairs_(France)	2010-02-24
Doc 5: (line 254376, score 0.946) France	Express_intent_to_meet_or_negotiate	Government_(France)	2010-09-08
Doc 6: (line 174259, score 0.944) France	Express_intent_to_meet_or_negotiate	Representatives_(France)	2008-10-30
Doc 7: (line 358908, score 0.944) Foreign_Affairs_(France)	Express_intent_to_meet_or_negotiate	France	2013-08-04
Doc 8: (line 441803, score 0.941) Foreign_Affairs_(France)	Express_intent_to_meet_or_negotiate	France	2015-07-17
Doc 9: (line 158365, score 0.939) France	Express_intent_to_meet_or_negotiate	Representatives_(France)	2008-06-23
Doc 10: (line 126618, score 0.936) Foreign_Affairs_(France)	Express_intent_to_meet_or_negotiate	France	2007-10-10 </information>
<answer>Ministry (International)</answer>

[Correct example 2]
Question: After Donald Rumsfeld, who was the first to want to meet with France?
Gold: Javier Solana

<think> I'll search for information about Donald Rumsfeld wanting to meet with France, then find who wanted to meet with France after him.

 </think>
<search> Donald Rumsfeld Express intent to meet or negotiate France sort=relevance entity=Donald Rumsfeld pos=front rel=Express_intent_to_meet_or_negotiate </search>
<information> Doc 1: (line 3815, score 0.955) Donald_Rumsfeld	Express_intent_to_meet_or_negotiate	France	2005-02-08
Doc 2: (line 41633, score 0.908) Donald_Rumsfeld	Express_intent_to_meet_or_negotiate	Iraq	2005-12-11
Doc 3: (line 41364, score 0.907) Donald_Rumsfeld	Express_intent_to_meet_or_negotiate	Iraq	2005-12-09
Doc 4: (line 41158, score 0.906) Donald_Rumsfeld	Express_intent_to_meet_or_negotiate	Iraq	2005-12-08
Doc 5: (line 88695, score 0.906) Donald_Rumsfeld	Express_intent_to_meet_or_negotiate	Iraq	2006-12-10
Doc 6: (line 62853, score 0.905) Donald_Rumsfeld	Express_intent_to_meet_or_negotiate	Vietnam	2006-05-29
Doc 7: (line 63147, score 0.905) Donald_Rumsfeld	Express_intent_to_meet_or_negotiate	Vietnam	2006-05-31
Doc 8: (line 43057, score 0.905) Donald_Rumsfeld	Express_intent_to_meet_or_negotiate	Iraq	2005-12-22
Doc 9: (line 78398, score 0.905) Donald_Rumsfeld	Express_intent_to_meet_or_negotiate	Fatmir_Mediu	2006-09-26
Doc 10: (line 65021, score 0.904) Donald_Rumsfeld	Express_intent_to_meet_or_negotiate	Iraq	2006-06-14 </information>
<think> I found that Donald Rumsfeld expressed intent to meet with France on February 8, 2005. Now I need to find who was the first to want to meet with France after this date. Let me search for entities expressing intent to meet with France after February 8, 2005.

 </think>
<search> Express intent to meet or negotiate France sort=time_asc start=2005-02-09 entity=France pos=back rel=Express_intent_to_meet_or_negotiate </search>
<information> Doc 1: (line 5406, score 0.871) Foreign_Affairs_(France)	Express_intent_to_meet_or_negotiate	France	2005-02-22
Doc 2: (line 12735, score 0.880) Men_(France)	Express_intent_to_meet_or_negotiate	France	2005-04-21
Doc 3: (line 44661, score 0.880) Foreign_Affairs_(France)	Express_intent_to_meet_or_negotiate	France	2006-01-09
Doc 4: (line 46797, score 0.864) Representatives_(France)	Express_intent_to_meet_or_negotiate	France	2006-01-25
Doc 5: (line 47416, score 0.881) Foreign_Affairs_(France)	Express_intent_to_meet_or_negotiate	France	2006-01-31
Doc 6: (line 52043, score 0.863) Japan	Express_intent_to_meet_or_negotiate	France	2006-03-09
Doc 7: (line 72685, score 0.867) Portugal	Express_intent_to_meet_or_negotiate	France	2006-08-15
Doc 8: (line 74730, score 0.862) China	Express_intent_to_meet_or_negotiate	France	2006-09-01
Doc 9: (line 76601, score 0.869) Representatives_(France)	Express_intent_to_meet_or_negotiate	France	2006-09-13
Doc 10: (line 79812, score 0.864) Portugal	Express_intent_to_meet_or_negotiate	France	2006-10-06 </information>
<think> The first entity to express intent to meet with France after Donald Rumsfeld (February 8, 2005) appears to be "Foreign Affairs (France)" on February 22, 2005. However, I should check if there are any other entities that expressed intent to meet with France between February 9 and February 22, 2005. Let me search more specifically for that time period.

 </think>
<search> Express intent to meet or negotiate France 2005-02-09 2005-02-21 sort=time_asc start=2005-02-09 end=2005-02-21 entity=France pos=back rel=Express_intent_to_meet_or_negotiate </search>
<information> Doc 1: (line 5062, score 0.877) Javier_Solana	Express_intent_to_meet_or_negotiate	France	2005-02-18
Doc 2: (line 5354, score 0.870) Royal_Administration_(Jordan)	Express_intent_to_meet_or_negotiate	France	2005-02-21 </information>
<answer>Javier Solana</answer>


[Correct example 3]
Question: Who praised the United Arab Emirates in the same month as Iran?
Gold: Muhammad VI

<think> I'll search for documents where someone praised the United Arab Emirates and Iran in the same month.

 </think>
<search> praised United Arab Emirates Iran same month sort=relevance </search>
<information> Doc 1: (line 194760, score 0.901) Iran	Praise_or_endorse	United_Arab_Emirates	2009-04-04
Doc 2: (line 131985, score 0.894) United_Arab_Emirates	Praise_or_endorse	Iran	2007-11-19
Doc 3: (line 266050, score 0.888) United_Arab_Emirates	Praise_or_endorse	Iran	2010-12-16
Doc 4: (line 292641, score 0.886) United_Arab_Emirates	Praise_or_endorse	Iran	2011-10-04
Doc 5: (line 369629, score 0.882) United_Arab_Emirates	Praise_or_endorse	Iran	2013-11-24
Doc 6: (line 406681, score 0.879) United_Arab_Emirates	Praise_or_endorse	Iran	2014-10-20
Doc 7: (line 248731, score 0.879) United_Arab_Emirates	Praise_or_endorse	Iran	2010-07-16
Doc 8: (line 302985, score 0.879) United_Arab_Emirates	Praise_or_endorse	Iran	2012-01-19
Doc 9: (line 141999, score 0.876) United_Arab_Emirates	Make_a_visit	Iran	2008-02-18
Doc 10: (line 371290, score 0.876) United_Arab_Emirates	Praise_or_endorse	Iran	2013-12-09 </information>
<think> I found several documents showing praise between Iran and the United Arab Emirates. Let me search more specifically for entities that praised the United Arab Emirates in the same month as Iran.

 </think>
<search> praise United Arab Emirates same month sort=relevance entity=United Arab Emirates pos=back rel=Praise_or_endorse </search>
<information> Doc 1: (line 197020, score 0.872) Muhammad_VI	Praise_or_endorse	United_Arab_Emirates	2009-04-16
Doc 2: (line 37755, score 0.859) Ministry_(Morocco)	Praise_or_endorse	United_Arab_Emirates	2005-11-14
Doc 3: (line 188048, score 0.856) Vietnam	Praise_or_endorse	United_Arab_Emirates	2009-02-18
Doc 4: (line 239322, score 0.855) Ali_Abdullah_Saleh	Praise_or_endorse	United_Arab_Emirates	2010-04-26
Doc 5: (line 187893, score 0.855) Vietnam	Praise_or_endorse	United_Arab_Emirates	2009-02-17
Doc 6: (line 239266, score 0.852) Japan	Praise_or_endorse	United_Arab_Emirates	2010-04-25
Doc 7: (line 100214, score 0.851) Sheikh_Ahmed_Abdullah	Praise_or_endorse	United_Arab_Emirates	2007-03-12
Doc 8: (line 104261, score 0.851) China	Praise_or_endorse	United_Arab_Emirates	2007-04-10
Doc 9: (line 94846, score 0.850) China	Praise_or_endorse	United_Arab_Emirates	2007-01-30
Doc 10: (line 362603, score 0.850) Kuwait	Praise_or_endorse	United_Arab_Emirates	2013-09-13 </information>
<think> Now let me check when Iran praised the United Arab Emirates to find the specific month, and then see who else praised the UAE in that same month.
From the first search result, I see Iran praised the United Arab Emirates on 2009-04-04. Let me check who else praised the UAE in April 2009.

 </think>
<search> praise United Arab Emirates April 2009 sort=relevance start=2009-04-01 end=2009-04-30 entity=United Arab Emirates pos=back rel=Praise_or_endorse </search>
<information> Doc 1: (line 197020, score 0.886) Muhammad_VI	Praise_or_endorse	United_Arab_Emirates	2009-04-16
Doc 2: (line 194760, score 0.859) Iran	Praise_or_endorse	United_Arab_Emirates	2009-04-04 </information>
<answer>Perfect! I found that in April 2009:
1. Iran praised the United Arab Emirates on April 4, 2009
2. Muhammad VI praised the United Arab Emirates on April 16, 2009
Therefore, Muhammad VI praised the United Arab Emirates in the same month (April 2009) as Iran.
<answer>Muhammad VI</answer></answer>

Notes (avoid these mistakes):
- For 'before X, who last ...', find the date of X first, then search with end_date just before that date and sort=time_desc.
- For 'after X, who first ...', search with start_date just after that date and sort=time_asc.
<!-- FEWSHOT_END -->
