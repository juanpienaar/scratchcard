import os
os.environ["DATABASE_URL"] = "sqlite:////tmp/sc_test.db"
import main as m


def test_strokes_received():
    # PH 0 -> no strokes anywhere
    assert all(m.strokes_received(0, si) == 0 for si in range(1, 19))
    # PH 18 -> exactly 1 on every hole
    assert all(m.strokes_received(18, si) == 1 for si in range(1, 19))
    # PH 9 -> 1 stroke on SI 1..9, 0 on 10..18
    assert m.strokes_received(9, 1) == 1
    assert m.strokes_received(9, 9) == 1
    assert m.strokes_received(9, 10) == 0
    # PH 22 -> 2 strokes on SI 1..4, 1 elsewhere
    assert m.strokes_received(22, 4) == 2
    assert m.strokes_received(22, 5) == 1
    # plus handicap PH -2 -> give back 1 on SI 18 and 17
    assert m.strokes_received(-2, 18) == -1
    assert m.strokes_received(-2, 17) == -1
    assert m.strokes_received(-2, 16) == 0
    print("strokes_received OK")


def test_stableford():
    # par 4, no strokes: gross 4 -> 2 pts, 3 -> 3, 5 -> 1, 6 -> 0, 7 -> 0
    assert m.stableford_points(4, 4, 0) == 2
    assert m.stableford_points(3, 4, 0) == 3
    assert m.stableford_points(5, 4, 0) == 1
    assert m.stableford_points(6, 4, 0) == 0
    assert m.stableford_points(7, 4, 0) == 0
    # with 1 stroke, gross 5 net 4 -> 2 pts
    assert m.stableford_points(5, 4, 1) == 2
    assert m.stableford_points(None, 4, 0) is None
    print("stableford OK")


def test_playing_handicaps():
    chs = {"a": 18, "b": 10, "c": 4}
    assert m.playing_handicaps(chs, "full") == {"a": 18, "b": 10, "c": 4}
    # off lowest: c=4 lowest -> 0, others differential
    assert m.playing_handicaps(chs, "off_lowest") == {"a": 14, "b": 6, "c": 0}
    # 90% of differential
    r = m.playing_handicaps(chs, "off_lowest_pct", 90)
    assert r == {"a": round(14 * .9), "b": round(6 * .9), "c": 0}, r
    print("playing_handicaps OK", r)


def test_course_handicap():
    # index 10, slope 113, rating 72, par 72 -> 10
    assert m.calc_course_handicap(10, 113, 72, 72) == 10
    # index 18, slope 130, rating 73, par 72 -> 18*130/113 + 1 = 20.7+1=21.7 -> 22
    assert m.calc_course_handicap(18, 130, 73, 72) == 22
    print("course_handicap OK")


def test_leaderboard_skins_and_betterball():
    from sqlalchemy.orm import Session
    m.Base.metadata.drop_all(bind=m.engine)
    m.Base.metadata.create_all(bind=m.engine)
    db = m.SessionLocal()
    import json
    par = [4] * 18
    si = list(range(1, 19))
    g = m.Game(token="t1", par_json=json.dumps(par), si_json=json.dumps(si),
               game_types_json=json.dumps(["individual_stableford", "better_ball_stableford", "skins"]),
               handicap_mode="full")
    db.add(g); db.commit(); db.refresh(g)
    # team 1: A(ch0), B(ch0); team 2: C(ch0), D(ch0)
    ps = []
    for nm, t in [("A", 1), ("B", 1), ("C", 2), ("D", 2)]:
        gp = m.GamePlayer(game_id=g.id, name=nm, course_handicap=0, team=t, group_no=1)
        db.add(gp); db.commit(); db.refresh(gp); ps.append(gp)
    A, B, C, D = ps
    # Hole 1: A=3,B=5,C=4,D=4 -> A wins skin (net). Team1 best=3pts(birdie), Team2 best=2
    # Hole 2: A=4,B=4,C=4,D=4 -> tie, carry
    # Hole 3: A=5,B=5,C=3,D=5 -> C wins skin worth 2 (carry from h2)
    grid = {
        1: {"A": 3, "B": 5, "C": 4, "D": 4},
        2: {"A": 4, "B": 4, "C": 4, "D": 4},
        3: {"A": 5, "B": 5, "C": 3, "D": 5},
    }
    name2gp = {"A": A, "B": B, "C": C, "D": D}
    for h, row in grid.items():
        for nm, gross in row.items():
            db.add(m.Score(game_id=g.id, game_player_id=name2gp[nm].id, hole=h, gross=gross))
    db.commit()
    board = m.build_leaderboard(g, ps, db.query(m.Score).all())

    # Skins check
    skins = {r["name"]: r["skins"] for r in board["skins"]["board"]}
    assert skins["A"] == 1, skins
    assert skins["C"] == 2, skins   # won hole 3 with carry from hole 2
    assert skins["B"] == 0 and skins["D"] == 0, skins
    # hole 1 winner A value 1
    assert board["skins"]["holes"][0]["winner"] == "A"
    assert board["skins"]["holes"][1]["carried"] is True
    assert board["skins"]["holes"][2]["winner"] == "C"
    assert board["skins"]["holes"][2]["value"] == 2

    # Better-ball team stableford: Team1 hole1 best=birdie(3), hole2 par(2), hole3 bogey(1) =6
    t1 = [r for r in board["teams"] if r["team"] == 1][0]
    assert t1["points"] == 6, t1
    # Team2: hole1 par(2), hole2 par(2), hole3 birdie(3) =7
    t2 = [r for r in board["teams"] if r["team"] == 2][0]
    assert t2["points"] == 7, t2
    assert board["teams"][0]["team"] == 2  # team2 leads

    # Individual: A has birdie+par+bogey =3+2+1=6
    a_row = [r for r in board["individual"] if r["name"] == "A"][0]
    assert a_row["points"] == 6, a_row
    db.close()
    print("leaderboard OK")


if __name__ == "__main__":
    test_strokes_received()
    test_stableford()
    test_playing_handicaps()
    test_course_handicap()
    test_leaderboard_skins_and_betterball()
    print("\nALL TESTS PASSED")
