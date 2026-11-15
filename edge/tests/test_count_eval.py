from rescue_edge.count_eval import FrameTruth, match_frame, parse_mot, score_sequence

GT = """1,7,10,10,10,20,1,1,0,0
1,8,100,100,10,20,1,2,0,0
1,9,300,300,50,50,0,0,0,0
2,7,12,10,10,20,1,1,0,0
2,8,100,102,10,20,1,1,0,0
3,7,14,10,10,20,1,1,0,0
3,8,100,104,10,20,1,1,0,0
3,5,200,200,10,20,0,1,0,0
"""


def test_parse_mot_people_and_ignore():
    t = parse_mot(GT)
    assert set(t[1].people) == {7, 8}
    assert t[1].ignore == [(300.0, 300.0, 350.0, 350.0)]
    assert 5 not in t[3].people        # score 0 => not a scored person


def test_match_frame_one_to_one_and_ignore():
    ft = FrameTruth({7: (10, 10, 20, 30)}, [(300, 300, 350, 350)])
    m = match_frame([(1, (10, 10, 20, 30)), (2, (11, 10, 21, 30)), (3, (310, 310, 320, 320))], ft)
    assert m == {1: 7, 2: None, 3: -1}


def _box(x, y):
    return (x, y, x + 10, y + 20)


def test_perfect_tracks_count_exactly():
    truth = parse_mot(GT)
    tracks = {f: [(1, _box(10 + 2 * (f - 1), 10)), (2, _box(100, 100 + 2 * (f - 1)))] for f in (1, 2, 3)}
    r = score_sequence(tracks, truth, min_frames=3)
    assert (r.gt_people, r.counted, r.found, r.false_tracks, r.duplicates) == (2, 2, 2, 0, 0)
    assert r.count_error == 0


def test_split_person_and_false_track_and_short_track():
    truth = parse_mot(GT)
    tracks = {
        1: [(1, _box(10, 10)), (4, _box(500, 500)), (6, _box(100, 100))],
        2: [(2, _box(12, 10)), (4, _box(500, 500))],
        3: [(2, _box(14, 10)), (4, _box(500, 500))],
    }
    # person 7: track 1 (1 frame, too short) then track 2 (2 frames, too short) => min_frames=2
    r = score_sequence(tracks, truth, min_frames=2)
    assert r.counted == 2 and r.found == 1 and r.false_tracks == 1 and r.duplicates == 0
    tracks[1].append((3, _box(10, 10)))
    tracks[2].append((3, _box(12, 10)))
    tracks[3].append((3, _box(14, 10)))
    r = score_sequence(tracks, truth, min_frames=2)
    # track 3 overlaps person 7 only when track 2 didn't take it first (one-to-one per frame)
    assert r.found == 1 and r.counted == 3
