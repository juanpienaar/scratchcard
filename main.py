from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from datetime import datetime
from typing import Optional, List
import os, json, uuid, base64, urllib.request, urllib.parse

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/scratchcard")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+pg8000://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+pg8000://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# ----------------------------- Models -----------------------------
class Player(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    handicap_index = Column(Float, default=0.0)


class Course(Base):
    __tablename__ = "courses"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    lat = Column(Float, nullable=True)
    lon = Column(Float, nullable=True)
    par_json = Column(Text, default="[]")      # list[18]
    si_json = Column(Text, default="[]")        # list[18]
    tees_json = Column(Text, default="[]")      # [{name,rating,slope}]
    is_favourite = Column(Integer, default=0)


class Game(Base):
    __tablename__ = "games"
    id = Column(Integer, primary_key=True)
    token = Column(String(40), unique=True, index=True)
    name = Column(String(200), default="Round")
    course_name = Column(String(200), default="")
    par_json = Column(Text, default="[]")
    si_json = Column(Text, default="[]")
    tee_name = Column(String(80), default="")
    rating = Column(Float, default=72.0)
    slope = Column(Float, default=113.0)
    game_types_json = Column(Text, default="[]")   # [main, side] subset
    main_game = Column(String(40), default="individual_stableford")
    side_game = Column(String(40), nullable=True)
    match_basis = Column(String(20), default="stableford")  # stableford | scratch
    handicap_mode = Column(String(40), default="full")  # full | off_lowest | off_lowest_pct
    handicap_pct = Column(Float, default=100.0)
    created_at = Column(String(40), default=lambda: datetime.utcnow().isoformat())


class GamePlayer(Base):
    __tablename__ = "game_players"
    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, index=True)
    player_id = Column(Integer, nullable=True)
    name = Column(String(120), nullable=False)
    handicap_index = Column(Float, default=0.0)
    course_handicap = Column(Integer, default=0)
    team = Column(Integer, nullable=True)   # team number for team games
    group_no = Column(Integer, default=1)   # fourball / group number


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (UniqueConstraint("game_player_id", "hole", name="uix_gp_hole"),)
    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, index=True)
    game_player_id = Column(Integer, index=True)
    hole = Column(Integer)
    gross = Column(Integer, nullable=True)


Base.metadata.create_all(bind=engine)


# ----------------------------- Scoring engine -----------------------------
def calc_course_handicap(index: float, slope: float, rating: float, par_total: int) -> int:
    """WHS course handicap (simplified, no PCC)."""
    return round(index * (slope / 113.0) + (rating - par_total))


def strokes_received(playing_handicap: int, stroke_index: int) -> int:
    """Strokes a player gets on a hole of the given stroke index (1=hardest)."""
    ph = int(playing_handicap)
    if ph >= 0:
        base, rem = divmod(ph, 18)
        return base + (1 if stroke_index <= rem else 0)
    # plus handicap: give strokes back starting at the easiest hole (SI 18)
    ph = -ph
    base, rem = divmod(ph, 18)
    return -(base + (1 if stroke_index > (18 - rem) else 0))


def stableford_points(gross: Optional[int], par: int, strokes: int) -> Optional[int]:
    if gross is None:
        return None
    net = gross - strokes
    return max(0, 2 - (net - par))


def net_score(gross: Optional[int], strokes: int) -> Optional[int]:
    if gross is None:
        return None
    return gross - strokes


def playing_handicaps(course_handicaps: dict, mode: str, pct: float = 100.0) -> dict:
    """Return dict {player_key: playing_handicap}."""
    if not course_handicaps:
        return {}
    if mode == "full":
        return {k: round(v) for k, v in course_handicaps.items()}
    low = min(course_handicaps.values())
    if mode == "off_lowest":
        return {k: round(v - low) for k, v in course_handicaps.items()}
    if mode == "off_lowest_pct":
        return {k: round((v - low) * pct / 100.0) for k, v in course_handicaps.items()}
    return {k: round(v) for k, v in course_handicaps.items()}


def build_leaderboard(game: Game, players: List[GamePlayer], scores: List[Score]) -> dict:
    par = json.loads(game.par_json) or [4] * 18
    si = json.loads(game.si_json) or list(range(1, 19))
    types = json.loads(game.game_types_json) or ["individual_stableford"]
    chs = {p.id: p.course_handicap for p in players}
    phs = playing_handicaps(chs, game.handicap_mode, game.handicap_pct)

    # index scores: {gp_id: {hole: gross}}
    smap = {}
    for s in scores:
        smap.setdefault(s.game_player_id, {})[s.hole] = s.gross

    # per-player per-hole points + net
    pdata = {}
    for p in players:
        ph = phs.get(p.id, p.course_handicap)
        holes = []
        total_pts = 0
        thru = 0
        gross_total = 0
        for h in range(1, 19):
            par_h = par[h - 1] if h - 1 < len(par) else 4
            si_h = si[h - 1] if h - 1 < len(si) else h
            strk = strokes_received(ph, si_h)
            gross = smap.get(p.id, {}).get(h)
            pts = stableford_points(gross, par_h, strk)
            net = net_score(gross, strk)
            if gross is not None:
                total_pts += (pts or 0)
                thru += 1
                gross_total += gross
            holes.append({"hole": h, "par": par_h, "si": si_h, "strokes": strk,
                          "gross": gross, "net": net, "points": pts})
        pdata[p.id] = {
            "game_player_id": p.id, "name": p.name, "team": p.team,
            "group_no": p.group_no, "course_handicap": p.course_handicap,
            "playing_handicap": ph, "holes": holes,
            "points": total_pts, "thru": thru, "gross": gross_total,
        }

    result = {"par": par, "si": si, "game_types": types,
              "individual": [], "teams": [], "skins": None, "matchplay": None}

    # Individual Stableford leaderboard (all players, across groups)
    indiv = sorted(pdata.values(), key=lambda d: (-d["points"], d["name"]))
    result["individual"] = indiv

    # Better-ball team Stableford
    if "better_ball_stableford" in types:
        teams = {}
        for p in players:
            if p.team is None:
                continue
            teams.setdefault(p.team, []).append(p.id)
        team_rows = []
        for tno, ids in teams.items():
            total = 0
            holes = []
            for h in range(1, 19):
                best = None
                for pid in ids:
                    pts = pdata[pid]["holes"][h - 1]["points"]
                    if pts is None:
                        continue
                    best = pts if best is None else max(best, pts)
                if best is not None:
                    total += best
                holes.append({"hole": h, "points": best})
            members = [pdata[pid]["name"] for pid in ids]
            team_rows.append({"team": tno, "members": members, "points": total, "holes": holes})
        result["teams"] = sorted(team_rows, key=lambda d: -d["points"])

    # Skins (net, lowest unique wins, carry on tie)
    if "skins" in types:
        skin_holes = []
        carry = 0
        won = {p.id: 0 for p in players}
        for h in range(1, 19):
            nets = {}
            complete = True
            for p in players:
                n = pdata[p.id]["holes"][h - 1]["net"]
                if n is None:
                    complete = False
                    break
                nets[p.id] = n
            if not complete or not nets:
                skin_holes.append({"hole": h, "winner": None, "value": None, "pending": True})
                continue
            low = min(nets.values())
            winners = [pid for pid, n in nets.items() if n == low]
            pot = carry + 1
            if len(winners) == 1:
                won[winners[0]] += pot
                carry = 0
                skin_holes.append({"hole": h, "winner": pdata[winners[0]]["name"],
                                   "value": pot, "pending": False})
            else:
                carry = pot
                skin_holes.append({"hole": h, "winner": None, "value": pot,
                                   "pending": False, "carried": True})
        skin_board = sorted(
            [{"name": pdata[p.id]["name"], "skins": won[p.id]} for p in players],
            key=lambda d: -d["skins"])
        result["skins"] = {"holes": skin_holes, "board": skin_board, "carry": carry}

    # Match play (2 sides: two teams, or two players)
    if "match_play" in types:
        result["matchplay"] = build_matchplay(pdata, players, game.match_basis or "stableford")

    return result


def build_matchplay(pdata: dict, players: List[GamePlayer], basis: str) -> dict:
    # Determine the two sides
    teams = {}
    for p in players:
        if p.team is not None:
            teams.setdefault(p.team, []).append(p.id)
    if len(teams) >= 2:
        tks = sorted(teams.keys())[:2]
        sides = [{"label": f"Team {tks[0]}", "ids": teams[tks[0]]},
                 {"label": f"Team {tks[1]}", "ids": teams[tks[1]]}]
    elif len(players) == 2:
        sides = [{"label": players[0].name, "ids": [players[0].id]},
                 {"label": players[1].name, "ids": [players[1].id]}]
    else:
        return {"error": "Match play needs exactly 2 players or 2 teams."}

    def side_value(ids, h):
        # best contribution of the side on hole h (1-based)
        vals = []
        for pid in ids:
            cell = pdata[pid]["holes"][h - 1]
            if cell["gross"] is None:
                continue
            if basis == "scratch":
                vals.append(cell["gross"])      # gross strokes, lower better
            else:
                vals.append(cell["points"])     # stableford pts, higher better
        if not vals:
            return None
        return min(vals) if basis == "scratch" else max(vals)

    a_wins = b_wins = halves = thru = 0
    hole_results = []
    for h in range(1, 19):
        va = side_value(sides[0]["ids"], h)
        vb = side_value(sides[1]["ids"], h)
        if va is None or vb is None:
            break   # match is sequential; stop at first unfinished hole
        thru += 1
        if va == vb:
            halves += 1
            res = "halved"
        elif (va < vb) == (basis == "scratch"):
            a_wins += 1
            res = "A"
        else:
            b_wins += 1
            res = "B"
        hole_results.append({"hole": h, "result": res})

    up = a_wins - b_wins
    remaining = 18 - thru
    leader = sides[0]["label"] if up > 0 else sides[1]["label"]
    if thru == 0:
        status = "Not started"
    elif abs(up) > remaining and up != 0:
        status = f"{leader} win {abs(up)} & {remaining}" if remaining > 0 else f"{leader} win {abs(up)} up"
    elif thru == 18:
        status = "Match halved" if up == 0 else f"{leader} win {abs(up)} up"
    else:
        if up == 0:
            status = f"All square thru {thru}"
        else:
            status = f"{leader} {abs(up)} up thru {thru}"

    return {
        "basis": basis,
        "sideA": sides[0]["label"], "sideB": sides[1]["label"],
        "a_wins": a_wins, "b_wins": b_wins, "halves": halves,
        "thru": thru, "status": status, "holes": hole_results,
    }


# ----------------------------- App -----------------------------
app = FastAPI()


@app.on_event("startup")
def startup():
    with engine.connect() as conn:
        for stmt in [
            "ALTER TABLE games ADD COLUMN handicap_pct FLOAT DEFAULT 100.0",
            "ALTER TABLE game_players ADD COLUMN group_no INTEGER DEFAULT 1",
            "ALTER TABLE courses ADD COLUMN is_favourite INTEGER DEFAULT 0",
            "ALTER TABLE games ADD COLUMN main_game VARCHAR(40) DEFAULT 'individual_stableford'",
            "ALTER TABLE games ADD COLUMN side_game VARCHAR(40)",
            "ALTER TABLE games ADD COLUMN match_basis VARCHAR(20) DEFAULT 'stableford'",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()


app.mount("/static", StaticFiles(directory="static"), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def root():
    with open("static/index.html") as f:
        return f.read()


@app.get("/g/{token}", response_class=HTMLResponse)
def shared(token: str):
    with open("static/index.html") as f:
        return f.read()


# ---- Players ----
class PlayerIn(BaseModel):
    name: str
    handicap_index: float = 0.0


@app.get("/api/players")
def list_players(db: Session = Depends(get_db)):
    return [{"id": p.id, "name": p.name, "handicap_index": p.handicap_index}
            for p in db.query(Player).order_by(Player.name).all()]


@app.post("/api/players")
def create_player(body: PlayerIn, db: Session = Depends(get_db)):
    p = Player(name=body.name.strip(), handicap_index=body.handicap_index)
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"id": p.id, "name": p.name, "handicap_index": p.handicap_index}


# ---- Courses ----
class CourseIn(BaseModel):
    name: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    par: List[int] = []
    si: List[int] = []
    tees: List[dict] = []


@app.get("/api/courses")
def list_courses(db: Session = Depends(get_db)):
    out = []
    courses = db.query(Course).order_by(Course.is_favourite.desc(), Course.name).all()
    for c in courses:
        out.append({"id": c.id, "name": c.name, "lat": c.lat, "lon": c.lon,
                    "par": json.loads(c.par_json), "si": json.loads(c.si_json),
                    "tees": json.loads(c.tees_json),
                    "is_favourite": bool(c.is_favourite)})
    return out


@app.post("/api/courses")
def create_course(body: CourseIn, db: Session = Depends(get_db)):
    # de-dup by name: update existing rather than create a second copy
    c = db.query(Course).filter(Course.name == body.name.strip()).first()
    if not c:
        c = Course(name=body.name.strip())
        db.add(c)
    c.lat = body.lat
    c.lon = body.lon
    c.par_json = json.dumps(body.par)
    c.si_json = json.dumps(body.si)
    c.tees_json = json.dumps(body.tees)
    db.commit()
    db.refresh(c)
    return {"id": c.id, "is_favourite": bool(c.is_favourite)}


@app.post("/api/courses/{cid}/favourite")
def toggle_favourite(cid: int, db: Session = Depends(get_db)):
    c = db.query(Course).filter(Course.id == cid).first()
    if not c:
        raise HTTPException(404, "Course not found")
    c.is_favourite = 0 if c.is_favourite else 1
    db.commit()
    return {"id": c.id, "is_favourite": bool(c.is_favourite)}


def parse_scorecard(image_bytes: bytes, media_type: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    b64 = base64.standard_b64encode(image_bytes).decode()
    prompt = (
        "This is a photo of a golf scorecard. Read it carefully and extract:\n"
        "- the course/club name\n"
        "- for each of the 18 holes: its par and its stroke index (also called "
        "handicap or 'HCP' / 'SI' on the card)\n"
        "- any tee sets shown, with course rating and slope rating if visible\n\n"
        "Return ONLY valid JSON, no markdown, no explanation, in exactly this shape:\n"
        '{"name": string, "par": [18 integers], "si": [18 integers], '
        '"tees": [{"name": string, "rating": number, "slope": number}]}\n'
        "If the card only shows 9 holes, still return what you can. "
        "If a value is not visible, use null. Stroke index values are 1-18, each used once."
    )
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type, "data": b64}},
            {"type": "text", "text": prompt},
        ]}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```"))
    return json.loads(raw)


@app.post("/api/courses/parse-photo")
async def parse_photo(file: UploadFile = File(...)):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY not set on the server")
    content = await file.read()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(413, "Image too large (max 8MB)")
    media_type = file.content_type or "image/jpeg"
    if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        media_type = "image/jpeg"
    try:
        data = parse_scorecard(content, media_type)
    except Exception as e:
        raise HTTPException(502, f"Could not read scorecard: {e}")
    return data


@app.get("/api/courses/nearby")
def nearby_courses(lat: float, lon: float, radius: int = 30000):
    """Find golf courses near a coordinate via OpenStreetMap Overpass API."""
    q = f"""[out:json][timeout:25];
(
  node["leisure"="golf_course"](around:{radius},{lat},{lon});
  way["leisure"="golf_course"](around:{radius},{lat},{lon});
  relation["leisure"="golf_course"](around:{radius},{lat},{lon});
);
out center tags 40;"""
    data = urllib.parse.urlencode({"data": q}).encode()
    try:
        req = urllib.request.Request(
            "https://overpass-api.de/api/interpreter", data=data,
            headers={"User-Agent": "ScratchCard/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            js = json.loads(r.read().decode())
    except Exception as e:
        raise HTTPException(502, f"Course lookup failed: {e}")
    seen = {}
    for el in js.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        clat = el.get("lat") or el.get("center", {}).get("lat")
        clon = el.get("lon") or el.get("center", {}).get("lon")
        if name not in seen:
            seen[name] = {"name": name, "lat": clat, "lon": clon}
    return list(seen.values())[:40]


# ---- Games ----
class GamePlayerIn(BaseModel):
    player_id: Optional[int] = None
    name: str
    handicap_index: float = 0.0
    course_handicap: int = 0
    team: Optional[int] = None
    group_no: int = 1


class GameIn(BaseModel):
    name: str = "Round"
    course_name: str = ""
    par: List[int] = []
    si: List[int] = []
    tee_name: str = ""
    rating: float = 72.0
    slope: float = 113.0
    main_game: str = "individual_stableford"
    side_game: Optional[str] = None
    match_basis: str = "stableford"
    game_types: List[str] = []
    handicap_mode: str = "full"
    handicap_pct: float = 100.0
    players: List[GamePlayerIn] = []


@app.post("/api/games")
def create_game(body: GameIn, db: Session = Depends(get_db)):
    token = uuid.uuid4().hex[:10]
    main = body.main_game or "individual_stableford"
    side = body.side_game if body.side_game and body.side_game != main else None
    types = [main] + ([side] if side else [])
    g = Game(token=token, name=body.name, course_name=body.course_name,
             par_json=json.dumps(body.par), si_json=json.dumps(body.si),
             tee_name=body.tee_name, rating=body.rating, slope=body.slope,
             game_types_json=json.dumps(types), main_game=main, side_game=side,
             match_basis=body.match_basis or "stableford",
             handicap_mode=body.handicap_mode, handicap_pct=body.handicap_pct)
    db.add(g)
    db.commit()
    db.refresh(g)
    for pin in body.players:
        gp = GamePlayer(game_id=g.id, player_id=pin.player_id, name=pin.name.strip(),
                        handicap_index=pin.handicap_index, course_handicap=pin.course_handicap,
                        team=pin.team, group_no=pin.group_no)
        db.add(gp)
    db.commit()
    return {"token": token}


def _game_state(token: str, db: Session) -> dict:
    g = db.query(Game).filter(Game.token == token).first()
    if not g:
        raise HTTPException(404, "Game not found")
    players = db.query(GamePlayer).filter(GamePlayer.game_id == g.id).all()
    scores = db.query(Score).filter(Score.game_id == g.id).all()
    board = build_leaderboard(g, players, scores)
    return {
        "token": g.token, "name": g.name, "course_name": g.course_name,
        "tee_name": g.tee_name, "rating": g.rating, "slope": g.slope,
        "game_types": json.loads(g.game_types_json),
        "main_game": g.main_game, "side_game": g.side_game,
        "match_basis": g.match_basis,
        "handicap_mode": g.handicap_mode, "handicap_pct": g.handicap_pct,
        "par": json.loads(g.par_json), "si": json.loads(g.si_json),
        "players": [{"id": p.id, "name": p.name, "handicap_index": p.handicap_index,
                     "course_handicap": p.course_handicap, "team": p.team,
                     "group_no": p.group_no} for p in players],
        "leaderboard": board,
    }


@app.get("/api/games/{token}")
def get_game(token: str, db: Session = Depends(get_db)):
    return _game_state(token, db)


class ScoreIn(BaseModel):
    game_player_id: int
    hole: int
    gross: Optional[int] = None


@app.post("/api/games/{token}/scores")
def upsert_score(token: str, body: ScoreIn, db: Session = Depends(get_db)):
    g = db.query(Game).filter(Game.token == token).first()
    if not g:
        raise HTTPException(404, "Game not found")
    s = db.query(Score).filter(Score.game_player_id == body.game_player_id,
                               Score.hole == body.hole).first()
    if s:
        s.gross = body.gross
    else:
        s = Score(game_id=g.id, game_player_id=body.game_player_id,
                  hole=body.hole, gross=body.gross)
        db.add(s)
    db.commit()
    return {"ok": True}


@app.get("/api/games/{token}/leaderboard")
def leaderboard(token: str, db: Session = Depends(get_db)):
    return _game_state(token, db)["leaderboard"]
