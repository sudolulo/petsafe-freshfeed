import unittest
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.petsafe_freshfeed import observed as o

TZ = ZoneInfo('America/New_York')

def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def event(kind, at, compartment=None):
    return o.FeedEvent(kind, compartment, dt(at))

class ObservedTests(unittest.TestCase):
    def test_parse_int_variants(self):
        for value, expected in [('6', 6), ('6.0', 6), (' 3 ', 3), ('6.5', None), ('abc', None), (None, None), ('unknown', None), ('unavailable', None), ('', None)]:
            with self.subTest(value=value): self.assertEqual(o.parse_int(value), expected)

    def test_snapshot_live_values(self):
        s = o.snapshot({4:'standby',14:'9',110:'on',111:'10',125:'on',130:'2.5.18',131:'7.1.0.0',142:'0',145:'6',150:'0'})
        self.assertEqual((s.available,s.status,s.compartment,s.meals_left,s.food_level,s.fault,s.feed_code,s.defrost_minutes,s.defrosting,s.brightness,s.firmware_a,s.firmware_b), (True,'no_food',6,0,'empty',False,9,0,False,10,'2.5.18','7.1.0.0'))

    def test_food_levels(self):
        for c, left, level in [(1,5,'high'),(3,3,'high'),(4,2,'low'),(5,1,'low'),(6,0,'empty')]:
            s = o.snapshot({4:'standby',14:'1',145:str(c)})
            self.assertEqual((s.meals_left,s.food_level,s.status), (left,level,'standby' if left else 'no_food'))

    def test_out_of_range_compartment(self):
        for value in ('0','7','-1','abc'):
            s = o.snapshot({4:'standby',14:'1',145:value})
            self.assertEqual((s.compartment,s.meals_left,s.food_level,s.status), (None,None,None,'standby'))

    def test_unavailable_snapshot(self):
        for dp in (4,145):
            for value in (None,'unknown','unavailable'):
                raw = {4:'standby',14:'1',145:'2',111:'10'}
                if value is None: del raw[dp]
                else: raw[dp] = value
                s = o.snapshot(raw)
                self.assertEqual((s.available,s.status,s.brightness), (False,'offline',10))

    def test_optional_feed_code_unknown_or_missing(self):
        for value in (None, 'unknown'):
            raw = {4: 'standby', 145: '2'}
            if value is not None:
                raw[14] = value
            s = o.snapshot(raw)
            self.assertTrue(s.available)
            self.assertIsNone(s.feed_code)

    def test_fault_flag(self):
        for value, expected in [('1',True),('0',False),('unknown',None)]:
            self.assertIs(o.snapshot({150:value}).fault, expected)

    def test_classify_rules(self):
        at = dt('2026-11-07T17:00:21Z')
        for dp, old, new, kind, compartment in [(145,'2','3','dispensed',3),(145,'6','1','refilled',1),(145,'2','1','refilled',1),(145,'1','1',None,None),(145,'2','4',None,None),(145,'3','2',None,None),(145,'7','1',None,None),(14,'5','9','skipped_empty',None),(14,'3','10','skipped_fault',None),(14,'9','9',None,None),(14,'9','1',None,None),(142,'1','0',None,None)]:
            with self.subTest(dp=dp,old=old,new=new):
                result = o.classify(dp,old,new,at=at)
                self.assertEqual(result, o.FeedEvent(kind,compartment,at) if kind else None)

    def test_unavailable_transitions_are_not_events(self):
        for dp, valid in [(145,'3'),(14,'10')]:
            for bad in (None,'unknown','unavailable'):
                for old,new in [(bad,valid),(valid,bad),(bad,bad)]:
                    self.assertIsNone(o.classify(dp,old,new,at=dt('2026-11-07T12:00:00Z')))

    def replay(self):
        rows = [(145,'6','1','2026-11-04T15:08:00Z'),(145,'1','2','2026-11-04T17:00:00Z'),(145,'2','3','2026-11-05T00:00:00Z'),(145,'3','4','2026-11-05T12:00:00Z'),(145,'4','5','2026-11-05T17:00:00Z'),(145,'5','6','2026-11-06T00:00:00Z'),(14,'5','9','2026-11-06T12:00:00Z'),(145,'6','1','2026-11-06T14:38:00Z'),(145,'1','2','2026-11-06T17:00:00Z'),(145,'2','3','2026-11-07T00:00:00Z'),(14,'2','10','2026-11-07T12:00:26Z'),(145,'3','4','2026-11-07T17:00:21Z'),(145,'4','5','2026-11-08T00:00:00Z'),(145,'5','6','2026-11-08T12:00:00Z'),(14,'5','9','2026-11-08T17:00:01Z')]
        return [o.classify(dp,old,new,at=dt(at)) for dp,old,new,at in rows]

    def test_replay_recorder_timeline(self):
        self.assertEqual([e.kind for e in self.replay()], ['refilled']+['dispensed']*5+['skipped_empty','refilled','dispensed','dispensed','skipped_fault','dispensed','dispensed','dispensed','skipped_empty'])

    def test_infer_meal_times(self):
        self.assertEqual(o.infer_meal_times(self.replay(),now=dt('2026-11-10T20:00:00Z'),tz=TZ), [time(7),time(12),time(19)])

    def test_infer_meal_times_across_dst(self):
        events = [event('dispensed','2026-10-31T23:00:07Z'),event('dispensed','2026-11-02T00:00:07Z')]
        self.assertEqual(o.infer_meal_times(events,now=dt('2026-11-02T01:00:00Z'),tz=TZ),[time(19)])

    def test_infer_requires_two_dates_and_groups_within_3_minutes(self):
        now = dt('2026-11-10T20:00:00Z')
        events = [event('dispensed','2026-11-08T17:00:00Z'),event('skipped_empty','2026-11-09T17:02:00Z'),event('dispensed','2026-11-09T08:15:00Z'),event('refilled','2026-11-08T08:15:00Z'),event('dispensed','2026-11-01T08:15:00Z')]
        self.assertEqual(o.infer_meal_times(events,now=now,tz=TZ),[time(12)])
        self.assertEqual(o.infer_meal_times([events[0],event('dispensed','2026-11-09T17:04:00Z')],now=now,tz=TZ),[])


    def test_meals_today_and_last_of(self):
        events = [event('dispensed','2026-11-10T04:59:00Z'),event('dispensed','2026-11-10T12:00:00Z'),event('skipped_empty','2026-11-10T17:00:00Z'),event('dispensed','2026-11-10T18:00:00Z')]
        self.assertEqual(o.meals_today(events,now=dt('2026-11-10T20:00:00Z'),tz=TZ),2)
        self.assertEqual(o.last_of(reversed(events),{'dispensed'}),events[-1])

    def test_prune_and_roundtrip(self):
        now = dt('2026-11-10T20:00:00Z')
        events = [o.FeedEvent('dispensed',2,now-timedelta(minutes=i)) for i in range(510)] + [o.FeedEvent('dispensed',2,now-timedelta(days=8))]
        kept = o.prune(reversed(events),now=now)
        self.assertEqual(len(kept),500)
        self.assertEqual(set(kept),set(events[:500]))
        self.assertEqual(kept, list(reversed(events[:500])))
        self.assertEqual(o.prune([events[0], events[-1], events[1]],now=now), [events[1], events[0]])
        self.assertEqual(o.FeedEvent.from_dict(events[0].to_dict()),events[0])
        for bad in [{'kind':'other','compartment':1,'at':now.isoformat()}, {'kind':'dispensed','compartment':1,'at':'2026-11-10T20:00:00'}, {'kind':'dispensed'}, {'kind':'dispensed','compartment':'2','at':now.isoformat()}]:
            self.assertIsNone(o.FeedEvent.from_dict(bad))
