#!/usr/bin/env python3
"""Durak Online — Complete multiplayer card game. Run: python main.py"""

import subprocess, sys

def _ensure(pkg: str) -> None:
    try:
        __import__(pkg)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

for _p in ("fastapi", "uvicorn", "websockets"):
    _ensure(_p)

import asyncio
import json
import random
import string
import time
from typing import Optional, Dict, List, Set, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

SUITS: List[str] = ["hearts", "diamonds", "clubs", "spades"]
RANKS: List[str] = ["6", "7", "8", "9", "10", "J", "Q", "K", "A"]
RANK_VAL: Dict[str, int] = {r: i for i, r in enumerate(RANKS)}


class Card:
    __slots__ = ("suit", "rank")

    def __init__(self, suit: str, rank: str) -> None:
        self.suit = suit
        self.rank = rank

    @property
    def value(self) -> int:
        return RANK_VAL[self.rank]

    def beats(self, other: "Card", trump: str) -> bool:
        if self.suit == other.suit:
            return self.value > other.value
        return self.suit == trump and other.suit != trump

    def key(self) -> str:
        return f"{self.rank}_{self.suit}"

    def to_dict(self) -> Dict[str, str]:
        return {"s": self.suit, "r": self.rank}


class Deck:
    def __init__(self) -> None:
        self.cards: List[Card] = [Card(s, r) for s in SUITS for r in RANKS]
        random.shuffle(self.cards)
        self.trump_card: Card = self.cards[0]
        self.trump_suit: str = self.trump_card.suit

    def draw(self) -> Optional[Card]:
        return self.cards.pop() if self.cards else None

    def remaining(self) -> int:
        return len(self.cards)


class Player:
    def __init__(self, pid: str, name: str) -> None:
        self.id = pid
        self.name = name
        self.hand: List[Card] = []
        self.is_out: bool = False

    def find_card(self, key: str) -> Optional[Card]:
        for c in self.hand:
            if c.key() == key:
                return c
        return None

    def remove_card(self, key: str) -> Optional[Card]:
        for i, c in enumerate(self.hand):
            if c.key() == key:
                return self.hand.pop(i)
        return None

    def hand_dict(self) -> List[Dict[str, str]]:
        return [c.to_dict() for c in self.hand]


class TablePair:
    __slots__ = ("attack", "defense")

    def __init__(self, attack: Card) -> None:
        self.attack = attack
        self.defense: Optional[Card] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"a": self.attack.to_dict()}
        if self.defense:
            d["d"] = self.defense.to_dict()
        return d


class Game:
    def __init__(self, settings: Dict[str, Any]) -> None:
        self.settings = settings
        self.phase: str = "waiting"
        self.deck: Optional[Deck] = None
        self.trump_suit: str = ""
        self.trump_card: Optional[Card] = None
        self.players: List[Player] = []
        self.table: List[TablePair] = []
        self.discard_count: int = 0
        self.attacker_idx: int = 0
        self.defender_idx: int = 1
        self.passed: Set[str] = set()
        self.picking_up: bool = False
        self.finished: List[str] = []
        self.durak: Optional[str] = None
        self.max_pairs: int = 6
        self.transfer: bool = settings.get("transfer", False)
        self.log: List[Dict[str, Any]] = []

    def add_player(self, pid: str, name: str) -> None:
        self.players.append(Player(pid, name))

    def pidx(self, pid: str) -> Optional[int]:
        for i, p in enumerate(self.players):
            if p.id == pid:
                return i
        return None

    def next_active(self, fr: int) -> int:
        n = len(self.players)
        idx = (fr + 1) % n
        for _ in range(n):
            if not self.players[idx].is_out:
                return idx
            idx = (idx + 1) % n
        return fr

    def active_count(self) -> int:
        return sum(1 for p in self.players if not p.is_out)

    def table_ranks(self) -> Set[str]:
        r: Set[str] = set()
        for tp in self.table:
            r.add(tp.attack.rank)
            if tp.defense:
                r.add(tp.defense.rank)
        return r

    def unbeaten(self) -> List[TablePair]:
        return [tp for tp in self.table if tp.defense is None]

    def start(self) -> Dict[str, Any]:
        self.deck = Deck()
        self.trump_suit = self.deck.trump_suit
        self.trump_card = self.deck.trump_card
        for _ in range(6):
            for p in self.players:
                c = self.deck.draw()
                if c:
                    p.hand.append(c)
        lo, fi = 99, 0
        for i, p in enumerate(self.players):
            for c in p.hand:
                if c.suit == self.trump_suit and c.value < lo:
                    lo, fi = c.value, i
        self.attacker_idx = fi
        self.defender_idx = self.next_active(fi)
        self.phase = "playing"
        self.max_pairs = min(6, len(self.players[self.defender_idx].hand))
        self.log = [{"t": "start"}]
        return {"ok": True}

    def act(self, pid: str, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if self.phase != "playing":
            return {"error": "not_playing"}
        idx = self.pidx(pid)
        if idx is None or self.players[idx].is_out:
            return {"error": "cant_act"}
        self.log.clear()
        handlers = {
            "attack": self._attack,
            "defend": self._defend,
            "pick_up": self._pick_up,
            "done": self._done,
            "transfer": self._transfer,
        }
        h = handlers.get(action)
        if not h:
            return {"error": "bad_action"}
        if action in ("attack", "transfer"):
            return h(idx, data.get("card", ""))
        if action == "defend":
            return h(idx, data.get("card", ""), data.get("target", ""))
        return h(idx)

    def _attack(self, idx: int, ck: str) -> Dict[str, Any]:
        if idx == self.defender_idx:
            return {"error": "defender"}
        p = self.players[idx]
        ub = self.unbeaten()
        if ub and not self.picking_up:
            return {"error": "wait_def"}
        if not self.table and idx != self.attacker_idx:
            return {"error": "not_atk"}
        if self.table and p.id in self.passed:
            return {"error": "passed"}
        card = p.find_card(ck)
        if not card:
            return {"error": "no_card"}
        if self.table and card.rank not in self.table_ranks():
            return {"error": "bad_rank"}
        if len(self.table) >= self.max_pairs:
            return {"error": "full"}
        if not self.picking_up:
            dfn = self.players[self.defender_idx]
            if len(ub) + 1 > len(dfn.hand):
                return {"error": "def_few"}
        p.remove_card(ck)
        self.table.append(TablePair(card))
        self.passed.clear()
        self.log.append({"t": "atk", "i": idx, "c": card.to_dict()})
        self._check_out(idx)
        return {"ok": True}

    def _defend(self, idx: int, ck: str, tk: str) -> Dict[str, Any]:
        if idx != self.defender_idx:
            return {"error": "not_def"}
        if self.picking_up:
            return {"error": "pu"}
        p = self.players[idx]
        card = p.find_card(ck)
        if not card:
            return {"error": "no_card"}
        tp = None
        for pair in self.table:
            if pair.defense is None and pair.attack.key() == tk:
                tp = pair
                break
        if not tp:
            return {"error": "no_target"}
        if not card.beats(tp.attack, self.trump_suit):
            return {"error": "cant_beat"}
        p.remove_card(ck)
        tp.defense = card
        self.log.append({"t": "def", "i": idx, "c": card.to_dict(), "on": tp.attack.to_dict()})
        if not self.unbeaten():
            self._check_out(idx)
            if self._all_done():
                self._end_beaten()
        return {"ok": True}

    def _pick_up(self, idx: int) -> Dict[str, Any]:
        if idx != self.defender_idx:
            return {"error": "not_def"}
        if self.picking_up:
            return {"error": "already"}
        if not self.table:
            return {"error": "empty"}
        self.picking_up = True
        self.passed.clear()
        self.log.append({"t": "pu", "i": idx})
        if self._all_done():
            self._finish_pickup()
        return {"ok": True}

    def _done(self, idx: int) -> Dict[str, Any]:
        if idx == self.defender_idx:
            return {"error": "defender"}
        p = self.players[idx]
        if p.id in self.passed:
            return {"error": "passed"}
        if self.unbeaten() and not self.picking_up:
            return {"error": "unbeaten"}
        self.passed.add(p.id)
        self.log.append({"t": "done", "i": idx})
        if self._all_done():
            if self.picking_up:
                self._finish_pickup()
            elif not self.unbeaten():
                self._end_beaten()
        return {"ok": True}

    def _transfer(self, idx: int, ck: str) -> Dict[str, Any]:
        if idx != self.defender_idx:
            return {"error": "not_def"}
        if not self.transfer:
            return {"error": "disabled"}
        if self.picking_up:
            return {"error": "pu"}
        ub = self.unbeaten()
        if not ub:
            return {"error": "no_ub"}
        if any(tp.defense is not None for tp in self.table):
            return {"error": "already_defended"}
        rank = ub[0].attack.rank
        if not all(tp.attack.rank == rank for tp in ub):
            return {"error": "diff"}
        p = self.players[idx]
        card = p.find_card(ck)
        if not card or card.rank != rank:
            return {"error": "bad"}
        nd = self.next_active(idx)
        if nd == self.attacker_idx and self.active_count() <= 2:
            return {"error": "cant"}
        if nd == self.attacker_idx:
            nd = self.next_active(nd)
        if len(ub) + 1 > len(self.players[nd].hand):
            return {"error": "next_few"}
        p.remove_card(ck)
        self.table.append(TablePair(card))
        self.defender_idx = nd
        self.max_pairs = min(6, len(self.players[nd].hand))
        self.passed.clear()
        self.log.append({"t": "tr", "i": idx, "c": card.to_dict(), "nd": nd})
        self._check_out(idx)
        return {"ok": True}

    def _all_done(self) -> bool:
        if len(self.table) >= self.max_pairs:
            return True
        if not self.picking_up:
            dfn = self.players[self.defender_idx]
            if len(dfn.hand) == 0 and not self.unbeaten():
                return True
        for i, p in enumerate(self.players):
            if i == self.defender_idx or p.is_out or len(p.hand) == 0:
                continue
            if p.id not in self.passed:
                ranks = self.table_ranks()
                can = any(c.rank in ranks for c in p.hand) and len(self.table) < self.max_pairs
                if can:
                    return False
                self.passed.add(p.id)
        return True

    def _finish_pickup(self) -> None:
        dfn = self.players[self.defender_idx]
        for tp in self.table:
            dfn.hand.append(tp.attack)
            if tp.defense:
                dfn.hand.append(tp.defense)
        self.log.append({"t": "pu_done", "i": self.defender_idx})
        self.table.clear()
        self.picking_up = False
        self.passed.clear()
        self._draw()
        self._check_all_out()
        if self.phase != "playing":
            return
        od = self.defender_idx
        self.attacker_idx = self.next_active(od)
        if self.attacker_idx == od and self.active_count() > 1:
            self.attacker_idx = self.next_active(self.attacker_idx)
        self.defender_idx = self.next_active(self.attacker_idx)
        if self.defender_idx == self.attacker_idx:
            self._game_over()
        else:
            self.max_pairs = min(6, len(self.players[self.defender_idx].hand))

    def _end_beaten(self) -> None:
        cnt = sum(1 + (1 if tp.defense else 0) for tp in self.table)
        self.discard_count += cnt
        self.log.append({"t": "beaten", "n": cnt})
        self.table.clear()
        self.passed.clear()
        self._draw()
        self._check_all_out()
        if self.phase != "playing":
            return
        od = self.defender_idx
        self.attacker_idx = od if not self.players[od].is_out else self.next_active(od)
        self.defender_idx = self.next_active(self.attacker_idx)
        if self.defender_idx == self.attacker_idx:
            self._game_over()
        else:
            self.max_pairs = min(6, len(self.players[self.defender_idx].hand))

    def _draw(self) -> None:
        if not self.deck or self.deck.remaining() == 0:
            return
        order: List[int] = []
        idx = self.attacker_idx
        n = len(self.players)
        for _ in range(n):
            if idx != self.defender_idx:
                order.append(idx)
            idx = (idx + 1) % n
        order.append(self.defender_idx)
        draws: List[Dict[str, Any]] = []
        for pi in order:
            p = self.players[pi]
            if p.is_out:
                continue
            while len(p.hand) < 6 and self.deck.remaining() > 0:
                c = self.deck.draw()
                if c:
                    p.hand.append(c)
                    draws.append({"i": pi, "c": c.to_dict()})
        if draws:
            self.log.append({"t": "draw", "d": draws, "rem": self.deck.remaining()})

    def _check_out(self, idx: int) -> None:
        if self.deck and self.deck.remaining() > 0:
            return
        p = self.players[idx]
        if not p.is_out and len(p.hand) == 0:
            p.is_out = True
            if p.id not in self.finished:
                self.finished.append(p.id)

    def _check_all_out(self) -> None:
        if self.deck and self.deck.remaining() > 0:
            return
        for p in self.players:
            if not p.is_out and len(p.hand) == 0:
                p.is_out = True
                if p.id not in self.finished:
                    self.finished.append(p.id)
        if sum(1 for p in self.players if not p.is_out) <= 1:
            self._game_over()

    def _game_over(self) -> None:
        self.phase = "game_over"
        active = [i for i, p in enumerate(self.players) if not p.is_out]
        self.durak = self.players[active[0]].id if len(active) == 1 else None
        self.log.append({"t": "over", "durak": self.durak})

    def state_for(self, pid: str, spectator: bool = False) -> Dict[str, Any]:
        idx = None if spectator else self.pidx(pid)
        ps = []
        for i, p in enumerate(self.players):
            ps.append({
                "id": p.id, "n": p.name, "cc": len(p.hand),
                "out": p.is_out, "atk": i == self.attacker_idx,
                "def": i == self.defender_idx,
                "pass": p.id in self.passed,
            })
        return {
            "type": "game_state", "ts": self.trump_suit,
            "tc": self.trump_card.to_dict() if self.trump_card else None,
            "dc": self.deck.remaining() if self.deck else 0,
            "disc": self.discard_count,
            "tbl": [tp.to_dict() for tp in self.table],
            "ps": ps, "ai": self.attacker_idx, "di": self.defender_idx,
            "yi": idx if idx is not None else -1,
            "hand": self.players[idx].hand_dict() if idx is not None else [],
            "phase": self.phase, "pu": self.picking_up,
            "durak": self.durak, "fin": self.finished,
            "acts": self._actions(idx) if idx is not None else [],
            "pc": self._playable(idx) if idx is not None else [],
            "log": self.log, "tr": self.transfer,
        }

    def _actions(self, idx: Optional[int]) -> List[str]:
        if idx is None or self.players[idx].is_out or self.phase != "playing":
            return []
        acts: List[str] = []
        p = self.players[idx]
        ub = self.unbeaten()
        if idx == self.defender_idx:
            if not self.picking_up and ub:
                can_beat = any(
                    c.beats(tp.attack, self.trump_suit)
                    for tp in self.table if tp.defense is None
                    for c in p.hand
                )
                if can_beat:
                    acts.append("defend")
                acts.append("pick_up")
                if self.transfer and ub and not any(tp.defense for tp in self.table):
                    rk = ub[0].attack.rank
                    if all(tp.attack.rank == rk for tp in ub) and any(c.rank == rk for c in p.hand):
                        nd = self.next_active(idx)
                        ok = not (nd == self.attacker_idx and self.active_count() <= 2)
                        if ok:
                            tnd = nd if nd != self.attacker_idx else self.next_active(nd)
                            if len(ub) + 1 <= len(self.players[tnd].hand):
                                acts.append("transfer")
        else:
            if not ub or self.picking_up:
                if not self.table:
                    if idx == self.attacker_idx and p.hand:
                        acts.append("attack")
                else:
                    if p.id not in self.passed:
                        if len(self.table) < self.max_pairs:
                            ranks = self.table_ranks()
                            if any(c.rank in ranks for c in p.hand):
                                acts.append("attack")
                        acts.append("done")
        return acts

    def _playable(self, idx: Optional[int]) -> List[str]:
        if idx is None or self.players[idx].is_out or self.phase != "playing":
            return []
        p = self.players[idx]
        pc: Set[str] = set()
        if idx == self.defender_idx and not self.picking_up:
            for tp in self.table:
                if tp.defense is None:
                    for c in p.hand:
                        if c.beats(tp.attack, self.trump_suit):
                            pc.add(c.key())
            if self.transfer:
                ub = self.unbeaten()
                if ub and not any(tp.defense for tp in self.table):
                    rk = ub[0].attack.rank
                    if all(tp.attack.rank == rk for tp in ub):
                        for c in p.hand:
                            if c.rank == rk:
                                pc.add(c.key())
        elif idx != self.defender_idx:
            ub = self.unbeaten()
            can_add = not ub or self.picking_up
            if can_add:
                if not self.table and idx == self.attacker_idx:
                    pc = {c.key() for c in p.hand}
                elif self.table and p.id not in self.passed and len(self.table) < self.max_pairs:
                    ranks = self.table_ranks()
                    for c in p.hand:
                        if c.rank in ranks:
                            pc.add(c.key())
        return list(pc)

class Room:
    def __init__(self, rid: str, name: str, max_p: int, transfer: bool, creator: str) -> None:
        self.id = rid
        self.name = name
        self.max_players = max_p
        self.transfer = transfer
        self.state: str = "waiting"
        self.player_ids: List[str] = []
        self.player_names: Dict[str, str] = {}
        self.ready: Dict[str, bool] = {}
        self.game: Optional[Game] = None
        self.spectator_ids: Set[str] = set()
        self.dc_times: Dict[str, float] = {}
        self.created: float = time.time()

    def add_player(self, pid: str, name: str) -> bool:
        if pid in self.player_ids:
            if pid in self.dc_times:
                del self.dc_times[pid]
            return True
        if len(self.player_ids) >= self.max_players or self.state != "waiting":
            return False
        self.player_ids.append(pid)
        self.player_names[pid] = name
        self.ready[pid] = False
        return True

    def remove_player(self, pid: str) -> None:
        if pid in self.player_ids:
            self.player_ids.remove(pid)
        self.player_names.pop(pid, None)
        self.ready.pop(pid, None)

    def all_ready(self) -> bool:
        return len(self.player_ids) >= 2 and all(self.ready.get(p, False) for p in self.player_ids)

    def start_game(self) -> Dict[str, Any]:
        self.game = Game({"transfer": self.transfer})
        for pid in self.player_ids:
            self.game.add_player(pid, self.player_names.get(pid, "Player"))
        self.state = "playing"
        return self.game.start()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name,
            "players": len(self.player_ids), "max": self.max_players,
            "state": self.state, "transfer": self.transfer,
            "names": [self.player_names.get(p, "?") for p in self.player_ids],
        }


class RoomManager:
    def __init__(self) -> None:
        self.rooms: Dict[str, Room] = {}

    def create(self, name: str, max_p: int, transfer: bool, cid: str, cname: str) -> Room:
        rid = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        while rid in self.rooms:
            rid = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
        room = Room(rid, name or f"Room {rid}", min(max(max_p, 2), 4), transfer, cid)
        room.add_player(cid, cname)
        self.rooms[rid] = room
        return room

    def get(self, rid: str) -> Optional[Room]:
        return self.rooms.get(rid)

    def remove(self, rid: str) -> None:
        self.rooms.pop(rid, None)

    def lobby(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self.rooms.values() if r.state != "finished"]

    def cleanup(self) -> None:
        now = time.time()
        for rid in list(self.rooms):
            r = self.rooms[rid]
            if (r.state == "waiting" and not r.player_ids and now - r.created > 60) or \
               (r.state == "finished" and now - r.created > 120):
                del self.rooms[rid]

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Durak Online</title>
<style>
/* ── Reset & Variables ── */
:root {
    --bg:#1a1a2e;--bg2:#16213e;--bg3:#0f3460;--fg:#e8e8e8;--fg2:#aaa;
    --accent:#4caf50;--accent2:#ff9800;--danger:#ef5350;--info:#42a5f5;
    --card-w:64px;--card-h:92px;--card-r:7px;
    --red:#e53935;--black:#222;
    --felt1:#2e7d32;--felt2:#1b5e20;--felt3:#0d3b12;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--fg);
  display:flex;flex-direction:column;-webkit-tap-highlight-color:transparent;user-select:none}

/* ── Header ── */
header{display:flex;align-items:center;justify-content:space-between;padding:6px 14px;
  background:var(--bg2);border-bottom:1px solid rgba(255,255,255,.07);flex-shrink:0;height:44px;z-index:50}
.logo{font-size:18px;font-weight:700;letter-spacing:1px;background:linear-gradient(90deg,#ffd700,#ff9800);
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.hdr-r{display:flex;gap:8px}
.hdr-btn{background:rgba(255,255,255,.08);border:none;color:var(--fg);padding:5px 10px;border-radius:6px;
  cursor:pointer;font-size:14px;transition:background .2s}
.hdr-btn:hover{background:rgba(255,255,255,.15)}

/* ── Views ── */
main{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative}
.view{display:none;flex-direction:column;flex:1;overflow:hidden}
.view.active{display:flex}

/* ── Name Entry ── */
.name-box{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;gap:18px;padding:20px}
.name-box h2{font-size:28px;background:linear-gradient(90deg,#ffd700,#ff9800);
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.name-box .sub{color:var(--fg2);font-size:14px}
.name-form{display:flex;gap:10px}
.name-form input{padding:10px 16px;border-radius:8px;border:1px solid rgba(255,255,255,.15);
  background:var(--bg2);color:var(--fg);font-size:16px;width:200px;outline:none}
.name-form input:focus{border-color:var(--accent)}
.btn{padding:10px 22px;border-radius:8px;border:none;font-size:15px;font-weight:600;cursor:pointer;
  transition:all .2s;min-height:44px}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:#43a047;transform:translateY(-1px)}
.btn-secondary{background:var(--bg3);color:var(--fg)}
.btn-secondary:hover{background:#1a4a80}
.btn-danger{background:var(--danger);color:#fff}
.btn-sm{padding:6px 14px;font-size:13px;min-height:36px}

/* ── Lobby ── */
.lobby{display:flex;flex-direction:column;flex:1;overflow:hidden;padding:10px 14px;gap:12px}
.create-panel{background:var(--bg2);border-radius:12px;padding:14px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.create-panel input,.create-panel select{padding:8px 12px;border-radius:6px;border:1px solid rgba(255,255,255,.12);
  background:var(--bg);color:var(--fg);font-size:14px;outline:none}
.create-panel select{min-width:60px}
.cb-label{display:flex;align-items:center;gap:5px;font-size:13px;color:var(--fg2);cursor:pointer}
.cb-label input[type=checkbox]{accent-color:var(--accent);width:16px;height:16px}
.room-list{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding:4px 0}
.room-card{background:var(--bg2);border-radius:10px;padding:12px 14px;display:flex;align-items:center;
  justify-content:space-between;transition:background .2s}
.room-card:hover{background:var(--bg3)}
.rc-info{display:flex;flex-direction:column;gap:2px}
.rc-name{font-weight:600;font-size:15px}
.rc-meta{font-size:12px;color:var(--fg2)}
.rc-btns{display:flex;gap:6px}
.no-rooms{text-align:center;color:var(--fg2);padding:40px;font-size:15px}

/* ── Room Wait ── */
.room-wait{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;gap:16px;padding:20px}
.room-hdr{display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:center}
.room-code{background:var(--bg3);padding:5px 12px;border-radius:6px;font-family:monospace;font-size:16px;
  cursor:pointer;letter-spacing:2px}
.player-slots{display:flex;gap:12px;flex-wrap:wrap;justify-content:center}
.p-slot{width:120px;height:80px;border-radius:12px;background:var(--bg2);display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:4px;border:2px dashed rgba(255,255,255,.1);transition:all .3s}
.p-slot.filled{border-color:var(--accent);border-style:solid}
.p-slot.ready{background:rgba(76,175,80,.15)}
.p-slot .avatar{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:16px;color:#fff}
.p-slot .sname{font-size:13px;max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ── Game Container ── */
.game-container{display:flex;flex-direction:column;flex:1;overflow:hidden}
.opp-area{display:flex;justify-content:center;gap:10px;padding:6px 10px;flex-shrink:0;flex-wrap:wrap}
.opp-panel{display:flex;flex-direction:column;align-items:center;gap:3px;padding:6px 10px;
  border-radius:10px;background:rgba(255,255,255,.04);position:relative;transition:box-shadow .3s}
.opp-panel.is-atk{box-shadow:0 0 0 2px var(--accent)}
.opp-panel.is-def{box-shadow:0 0 0 2px var(--danger)}
.opp-panel.is-out{opacity:.4}
.opp-panel.active-glow{animation:glowPulse 1.5s infinite}
.opp-info{display:flex;align-items:center;gap:6px}
.opp-name{font-size:12px;max-width:70px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.opp-count{font-size:11px;color:var(--fg2)}
.opp-cards{display:flex;margin-top:2px}
.opp-card{width:28px;height:40px;border-radius:3px;flex-shrink:0;
  background:repeating-linear-gradient(45deg,#1a237e,#1a237e 3px,#283593 3px,#283593 6px);
  border:1px solid #0d47a1;box-shadow:0 1px 2px rgba(0,0,0,.3)}
.opp-card+.opp-card{margin-left:-16px}

/* ── Table / Felt ── */
.middle-area{flex:1;display:flex;align-items:center;justify-content:center;position:relative;
  background:radial-gradient(ellipse at center,var(--felt1) 0%,var(--felt2) 65%,var(--felt3) 100%);
  border-radius:16px;margin:0 6px;box-shadow:inset 0 0 40px rgba(0,0,0,.35);min-height:160px;overflow:hidden}
.middle-area.shake{animation:screenShake .15s ease-in-out}
.deck-area{position:absolute;left:12px;top:50%;transform:translateY(-50%);display:flex;align-items:center}
.trump-under{position:absolute;transform:rotate(90deg);left:-8px;top:4px;z-index:0;pointer-events:none}
.deck-stack{position:relative;z-index:1;display:flex;align-items:center;justify-content:center}
.deck-count{position:absolute;bottom:-20px;left:50%;transform:translateX(-50%);font-size:13px;font-weight:700;
  color:#fff;text-shadow:0 1px 3px rgba(0,0,0,.7)}
.deck-empty{font-size:32px;color:rgba(255,255,255,.2)}
.trump-ind{position:absolute;left:12px;bottom:8px;font-size:11px;color:rgba(255,255,255,.7);
  display:flex;align-items:center;gap:4px}
.trump-ind .hearts,.trump-ind .diamonds{color:var(--red)}
.trump-ind .clubs,.trump-ind .spades{color:#ccc}
.play-area{display:flex;flex-wrap:wrap;justify-content:center;align-items:center;gap:8px;
  padding:10px 90px;min-height:100px}
.table-pair{position:relative;width:calc(var(--card-w) + 16px);height:calc(var(--card-h) + 22px);flex-shrink:0}
.table-pair .atk-card{position:absolute;top:0;left:0}
.table-pair .def-card{position:absolute;top:18px;left:13px;transform:rotate(12deg)}
.discard-area{position:absolute;right:12px;top:50%;transform:translateY(-50%);width:50px;height:60px}
.disc-card{position:absolute;border-radius:var(--card-r)}
.disc-count{position:absolute;bottom:-18px;left:50%;transform:translateX(-50%);font-size:11px;color:rgba(255,255,255,.5)}

/* ── Cards ── */
.card{width:var(--card-w);height:var(--card-h);border-radius:var(--card-r);background:#fff;
  border:1px solid #ccc;box-shadow:0 2px 5px rgba(0,0,0,.18),0 1px 2px rgba(0,0,0,.12);
  position:relative;display:inline-flex;align-items:center;justify-content:center;
  cursor:default;transition:transform .18s ease,box-shadow .18s ease;flex-shrink:0;will-change:transform}
.card.face-down{background:repeating-linear-gradient(45deg,#1a237e,#1a237e 4px,#283593 4px,#283593 8px);
  border:2px solid #0d47a1;box-shadow:inset 0 0 0 2px #3949ab,0 2px 5px rgba(0,0,0,.3);cursor:default}
.card.face-down .corner,.card.face-down .center-suit{display:none}
.corner{position:absolute;display:flex;flex-direction:column;align-items:center;line-height:1;font-weight:700}
.tl{top:3px;left:4px;font-size:11px}
.br{bottom:3px;right:4px;font-size:11px;transform:rotate(180deg)}
.rk{font-size:inherit}
.st{font-size:9px}
.center-suit{font-size:26px;pointer-events:none}
.card.hearts,.card.diamonds{color:var(--red)}
.card.clubs,.card.spades{color:var(--black)}
.card.playable{border:2px solid var(--accent);cursor:pointer;
  box-shadow:0 0 8px rgba(76,175,80,.4),0 2px 5px rgba(0,0,0,.18)}
.card.playable:hover{transform:translateY(-16px)!important;z-index:100!important;
  box-shadow:0 8px 20px rgba(0,0,0,.3),0 0 12px rgba(76,175,80,.5)}
.card.targetable{outline:2px dashed var(--accent2);outline-offset:3px;cursor:pointer;animation:glowPulse 1.5s infinite}
.card.sel-target{outline:3px solid var(--danger);outline-offset:3px}

/* ── Player Hand ── */
.player-area{flex-shrink:0;padding:0 6px 8px;position:relative}
.p-info{text-align:center;padding:2px 0;font-size:12px;display:flex;align-items:center;justify-content:center;gap:8px}
.p-info .role-badge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.p-info .role-atk{background:var(--accent);color:#fff}
.p-info .role-def{background:var(--danger);color:#fff}
.player-hand{display:flex;justify-content:center;align-items:flex-end;padding:4px 10px;
  min-height:100px;overflow-x:auto;-webkit-overflow-scrolling:touch}
.player-hand .card{margin:0 -7px;transition:transform .15s ease,box-shadow .15s ease,margin-top .15s ease}
.player-hand .card:first-child{margin-left:0}
.player-hand .card:last-child{margin-right:0}

/* ── Action Bar ── */
.action-bar{display:flex;justify-content:center;gap:8px;padding:4px 10px;flex-shrink:0;min-height:40px;flex-wrap:wrap}
.status-bar{text-align:center;padding:2px;font-size:13px;flex-shrink:0;min-height:22px}
.turn-text{color:var(--accent);font-weight:700;animation:glowPulse 1.5s infinite}
.spec-label{color:var(--accent2);font-weight:600}

/* ── Overlay ── */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;display:flex;
  align-items:center;justify-content:center;flex-direction:column}
.overlay.hidden{display:none}
.go-content{text-align:center;padding:30px;position:relative}
.go-content .banner{font-size:36px;font-weight:900;margin:16px 0}
.go-content .banner.bounce-in{animation:bounceIn .8s ease-out}
.go-btns{display:flex;gap:10px;justify-content:center;margin-top:20px}
.dunce-cap{width:0;height:0;border-left:35px solid transparent;border-right:35px solid transparent;
  border-bottom:75px solid #ff6f00;margin:0 auto 10px;position:relative}
.dunce-cap::before{content:'';position:absolute;top:75px;left:-40px;width:80px;height:12px;
  background:#e65100;border-radius:50%}
.dunce-cap::after{content:'D';position:absolute;top:28px;left:50%;transform:translateX(-50%);
  font-size:22px;font-weight:900;color:#fff}
.crown{font-size:52px;margin-bottom:8px;animation:floatY 2s ease-in-out infinite}
.shimmer{background:linear-gradient(90deg,#ffd700,#ffa000,#ffd700,#ffa000,#ffd700);background-size:400% 100%;
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
  animation:shimmerMove 2.5s linear infinite}
.fire-badge{display:inline-block;padding:5px 14px;background:linear-gradient(135deg,#ff6d00,#ff3d00);
  color:#fff;border-radius:14px;font-weight:700;font-size:14px;animation:firePulse .5s ease-in-out infinite alternate;margin-top:8px}
.durak-counter{color:var(--fg2);font-size:14px;margin-top:8px}
.card-scatter{position:relative;height:80px;width:200px;margin:10px auto}
.scatter-card{position:absolute;left:50%;top:50%;animation:scatter .8s ease-out forwards}
.confetti{position:absolute;width:8px;height:8px;border-radius:2px;top:-10px;pointer-events:none;animation:confettiFall linear forwards}

/* ── Toast ── */
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(80px);
  background:var(--bg2);color:var(--fg);padding:10px 20px;border-radius:8px;font-size:14px;
  box-shadow:0 4px 12px rgba(0,0,0,.4);z-index:300;transition:transform .3s ease;pointer-events:none;white-space:nowrap}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.err{border-left:4px solid var(--danger)}

/* ── Emoji ── */
.emoji-toggle{position:fixed;bottom:80px;right:12px;width:40px;height:40px;border-radius:50%;
  background:var(--bg2);border:none;font-size:20px;cursor:pointer;z-index:60;
  display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,.3)}
.emoji-panel{position:fixed;bottom:125px;right:12px;background:var(--bg2);border-radius:10px;
  padding:8px;display:flex;flex-wrap:wrap;gap:4px;z-index:60;box-shadow:0 4px 12px rgba(0,0,0,.4)}
.emoji-panel.hidden{display:none}
.emoji-panel button{background:none;border:none;font-size:22px;cursor:pointer;padding:4px;
  border-radius:6px;transition:background .15s}
.emoji-panel button:hover{background:rgba(255,255,255,.1)}
.floating-emoji{position:fixed;font-size:36px;pointer-events:none;z-index:250;animation:floatUp 2s ease-out forwards}

/* ── Animations ── */
@keyframes glowPulse{0%,100%{box-shadow:0 0 0 0 rgba(76,175,80,.3)}50%{box-shadow:0 0 0 8px rgba(76,175,80,0)}}
@keyframes screenShake{0%,100%{transform:translate(0,0)}25%{transform:translate(-2px,1px)}50%{transform:translate(2px,-1px)}75%{transform:translate(-1px,2px)}}
@keyframes bounceIn{0%{transform:translateY(-60px) scale(.7);opacity:0}60%{transform:translateY(8px) scale(1.05);opacity:1}80%{transform:translateY(-4px) scale(.97)}100%{transform:translateY(0) scale(1)}}
@keyframes floatY{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
@keyframes shimmerMove{0%{background-position:100% 0}100%{background-position:-100% 0}}
@keyframes firePulse{from{transform:scale(1)}to{transform:scale(1.08)}}
@keyframes scatter{to{transform:translate(var(--sx),var(--sy)) rotate(var(--sr));opacity:0}}
@keyframes confettiFall{to{transform:translateY(100vh) rotate(720deg);opacity:0}}
@keyframes floatUp{0%{transform:translateY(0) scale(1);opacity:1}100%{transform:translateY(-90px) scale(1.4);opacity:0}}
@keyframes cardIn{from{transform:scale(.5) translateY(30px);opacity:0}to{transform:scale(1) translateY(0);opacity:1}}
@keyframes flipIn{0%{transform:rotate(90deg) rotateY(180deg)}100%{transform:rotate(90deg) rotateY(0)}}
@keyframes crumble{to{transform:scale(.3) rotate(30deg);opacity:0}}

/* ── Responsive ── */
@media(max-width:768px){
  :root{--card-w:54px;--card-h:78px}
  .play-area{padding:8px 65px;gap:5px}
  .opp-area{gap:6px;padding:4px 6px}
  .opp-panel{padding:4px 6px}
  .player-hand{justify-content:flex-start;padding:4px 12px}
  .player-hand .card{margin:0 -10px}
  .table-pair{width:calc(var(--card-w) + 12px);height:calc(var(--card-h) + 18px)}
  .table-pair .def-card{top:14px;left:10px}
  .name-form{flex-direction:column;align-items:stretch}
  .name-form input{width:100%}
  .create-panel{flex-direction:column;align-items:stretch}
}
@media(max-width:420px){
  :root{--card-w:46px;--card-h:66px}
  .corner{font-size:9px!important}
  .st{font-size:7px!important}
  .center-suit{font-size:18px!important}
  .opp-card{width:22px;height:32px}
  .opp-card+.opp-card{margin-left:-12px}
  .play-area{padding:6px 50px;gap:4px}
  .go-content .banner{font-size:26px}
}
</style>
</head>
<body>
<header>
  <div class="logo" data-i18n="title">Durak Online</div>
  <div class="hdr-r">
    <button class="hdr-btn" onclick="toggleLang()" id="langBtn">EN</button>
    <button class="hdr-btn" onclick="toggleSound()" id="soundBtn">&#128266;</button>
  </div>
</header>
<main id="main">
  <!-- Name Entry -->
  <div id="v-name" class="view active">
    <div class="name-box">
      <h2 data-i18n="title">Durak Online</h2>
      <div class="sub" data-i18n="subtitle">The Classic Russian Card Game</div>
      <div class="name-form">
        <input type="text" id="nameInput" maxlength="12" data-i18n-ph="enter_name" placeholder="Enter your name"
               onkeydown="if(event.key==='Enter')setName()">
        <button class="btn btn-primary" onclick="setName()" data-i18n="play">Play</button>
      </div>
    </div>
  </div>
  <!-- Lobby -->
  <div id="v-lobby" class="view">
    <div class="lobby">
      <div class="create-panel">
        <input id="roomNameInput" type="text" maxlength="20" data-i18n-ph="room_name" placeholder="Room Name">
        <select id="maxPSel"><option value="2">2</option><option value="3">3</option><option value="4">4</option></select>
        <label class="cb-label"><input type="checkbox" id="transferCb"><span data-i18n="transfer_mode">Transfer</span></label>
        <button class="btn btn-primary btn-sm" onclick="createRoom()" data-i18n="create">Create</button>
      </div>
      <div id="roomList" class="room-list"></div>
    </div>
  </div>
  <!-- Room Wait -->
  <div id="v-room" class="view">
    <div class="room-wait">
      <div class="room-hdr">
        <h3 id="roomTitle"></h3>
        <span id="roomCodeDisp" class="room-code" onclick="copyCode()"></span>
        <button class="btn btn-danger btn-sm" onclick="leaveRoom()" data-i18n="leave">Leave</button>
      </div>
      <div id="playerSlots" class="player-slots"></div>
      <button id="readyBtn" class="btn btn-primary" onclick="toggleReady()" data-i18n="ready">Ready</button>
    </div>
  </div>
  <!-- Game -->
  <div id="v-game" class="view">
    <div class="game-container" id="gameContainer">
      <div id="oppArea" class="opp-area"></div>
      <div class="middle-area" id="midArea">
        <div id="deckArea" class="deck-area"></div>
        <div id="playArea" class="play-area"></div>
        <div id="discardArea" class="discard-area"></div>
        <div class="trump-ind" id="trumpInd"></div>
      </div>
      <div id="statusBar" class="status-bar"></div>
      <div id="actionBar" class="action-bar"></div>
      <div class="player-area">
        <div id="pInfo" class="p-info"></div>
        <div id="playerHand" class="player-hand"></div>
      </div>
    </div>
    <button class="emoji-toggle" onclick="toggleEmoji()">&#128522;</button>
    <div id="emojiPanel" class="emoji-panel hidden">
      <button onclick="sendEmoji('\\u{1F44D}')">&#x1F44D;</button>
      <button onclick="sendEmoji('\\u{1F602}')">&#x1F602;</button>
      <button onclick="sendEmoji('\\u{1F622}')">&#x1F622;</button>
      <button onclick="sendEmoji('\\u{1F621}')">&#x1F621;</button>
      <button onclick="sendEmoji('\\u{1F389}')">&#x1F389;</button>
      <button onclick="sendEmoji('\\u{1F914}')">&#x1F914;</button>
    </div>
  </div>
</main>
<div id="overlay" class="overlay hidden"></div>
<div id="toast" class="toast"></div>
<div id="emojiFloat"></div>
<script>
'use strict';

/* ── I18N ── */
const I18N={
en:{title:"Durak Online",subtitle:"The Classic Russian Card Game",enter_name:"Enter your name",play:"Play",
create_room:"Create Room",room_name:"Room Name",players_count:"Players",transfer_mode:"Transfer",
create:"Create",join:"Join",watch:"Watch",ready:"Ready",not_ready:"Not Ready",waiting:"Waiting",
in_progress:"In Progress",leave:"Leave",your_turn:"Your Turn!",pick_up:"Pick Up",done:"Done (Bita!)",
transfer:"Transfer",trump:"Trump",deck:"Deck",durak:"DURAK! THE FOOL!",winner:"CHAMPION!",
draw_game:"DRAW!",game_over:"Game Over",play_again:"Play Again",back_to_lobby:"Back to Lobby",
no_rooms:"No active rooms. Create one!",select_target:"Tap attack card first",spectating:"Spectating",
times_fool:"Times as Fool",on_fire:"ON FIRE!",room_code:"Code",copied:"Copied!",
waiting_players:"Waiting for players...",disconnected:"Reconnecting...",invalid_move:"Invalid move",
full:"Full",cards_left:"cards",you:"(You)",attacking:"Attacking",defending:"Defending",
throwing:"Can throw in",select_card:"Select a card to transfer"},
ru:{title:"\\u0414\\u0443\\u0440\\u0430\\u043A \\u041E\\u043D\\u043B\\u0430\\u0439\\u043D",subtitle:"\\u041A\\u043B\\u0430\\u0441\\u0441\\u0438\\u0447\\u0435\\u0441\\u043A\\u0430\\u044F \\u043A\\u0430\\u0440\\u0442\\u043E\\u0447\\u043D\\u0430\\u044F \\u0438\\u0433\\u0440\\u0430",enter_name:"\\u0412\\u0432\\u0435\\u0434\\u0438\\u0442\\u0435 \\u0438\\u043C\\u044F",play:"\\u0418\\u0433\\u0440\\u0430\\u0442\\u044C",
create_room:"\\u0421\\u043E\\u0437\\u0434\\u0430\\u0442\\u044C \\u043A\\u043E\\u043C\\u043D\\u0430\\u0442\\u0443",room_name:"\\u041D\\u0430\\u0437\\u0432\\u0430\\u043D\\u0438\\u0435",players_count:"\\u0418\\u0433\\u0440\\u043E\\u043A\\u0438",transfer_mode:"\\u041F\\u0435\\u0440\\u0435\\u0432\\u043E\\u0434\\u043D\\u043E\\u0439",
create:"\\u0421\\u043E\\u0437\\u0434\\u0430\\u0442\\u044C",join:"\\u0412\\u043E\\u0439\\u0442\\u0438",watch:"\\u0421\\u043C\\u043E\\u0442\\u0440\\u0435\\u0442\\u044C",ready:"\\u0413\\u043E\\u0442\\u043E\\u0432",not_ready:"\\u041D\\u0435 \\u0433\\u043E\\u0442\\u043E\\u0432",waiting:"\\u041E\\u0436\\u0438\\u0434\\u0430\\u043D\\u0438\\u0435",
in_progress:"\\u0412 \\u0438\\u0433\\u0440\\u0435",leave:"\\u0412\\u044B\\u0439\\u0442\\u0438",your_turn:"\\u0412\\u0430\\u0448 \\u0445\\u043E\\u0434!",pick_up:"\\u0417\\u0430\\u0431\\u0440\\u0430\\u0442\\u044C",done:"\\u0411\\u0438\\u0442\\u0430!",
transfer:"\\u041F\\u0435\\u0440\\u0435\\u0432\\u0435\\u0441\\u0442\\u0438",trump:"\\u041A\\u043E\\u0437\\u044B\\u0440\\u044C",deck:"\\u041A\\u043E\\u043B\\u043E\\u0434\\u0430",durak:"\\u0414\\u0423\\u0420\\u0410\\u041A!",winner:"\\u041F\\u041E\\u0411\\u0415\\u0414\\u0418\\u0422\\u0415\\u041B\\u042C!",
draw_game:"\\u041D\\u0418\\u0427\\u042C\\u042F!",game_over:"\\u0418\\u0433\\u0440\\u0430 \\u043E\\u043A\\u043E\\u043D\\u0447\\u0435\\u043D\\u0430",play_again:"\\u0418\\u0433\\u0440\\u0430\\u0442\\u044C \\u0441\\u043D\\u043E\\u0432\\u0430",back_to_lobby:"\\u0412 \\u043B\\u043E\\u0431\\u0431\\u0438",
no_rooms:"\\u041D\\u0435\\u0442 \\u043A\\u043E\\u043C\\u043D\\u0430\\u0442. \\u0421\\u043E\\u0437\\u0434\\u0430\\u0439\\u0442\\u0435!",select_target:"\\u0412\\u044B\\u0431\\u0435\\u0440\\u0438\\u0442\\u0435 \\u043A\\u0430\\u0440\\u0442\\u0443 \\u0430\\u0442\\u0430\\u043A\\u0438",spectating:"\\u041D\\u0430\\u0431\\u043B\\u044E\\u0434\\u0430\\u0442\\u0435\\u043B\\u044C",
times_fool:"\\u0420\\u0430\\u0437 \\u0434\\u0443\\u0440\\u0430\\u043A\\u043E\\u043C",on_fire:"\\u0412 \\u0423\\u0414\\u0410\\u0420\\u0415!",room_code:"\\u041A\\u043E\\u0434",copied:"\\u0421\\u043A\\u043E\\u043F\\u0438\\u0440\\u043E\\u0432\\u0430\\u043D\\u043E!",
waiting_players:"\\u041E\\u0436\\u0438\\u0434\\u0430\\u043D\\u0438\\u0435 \\u0438\\u0433\\u0440\\u043E\\u043A\\u043E\\u0432...",disconnected:"\\u041F\\u0435\\u0440\\u0435\\u043F\\u043E\\u0434\\u043A\\u043B\\u044E\\u0447\\u0435\\u043D\\u0438\\u0435...",invalid_move:"\\u041D\\u0435\\u0434\\u043E\\u043F\\u0443\\u0441\\u0442\\u0438\\u043C\\u044B\\u0439 \\u0445\\u043E\\u0434",
full:"\\u0417\\u0430\\u043F\\u043E\\u043B\\u043D\\u0435\\u043D\\u043E",cards_left:"\\u043A\\u0430\\u0440\\u0442",you:"(\\u0412\\u044B)",attacking:"\\u0410\\u0442\\u0430\\u043A\\u0430",defending:"\\u0417\\u0430\\u0449\\u0438\\u0442\\u0430",
throwing:"\\u041C\\u043E\\u0436\\u043D\\u043E \\u043F\\u043E\\u0434\\u043A\\u0438\\u043D\\u0443\\u0442\\u044C",select_card:"\\u0412\\u044B\\u0431\\u0435\\u0440\\u0438\\u0442\\u0435 \\u043A\\u0430\\u0440\\u0442\\u0443"}
};

/* ── State ── */
const COLORS=['#e53935','#1e88e5','#43a047','#fb8c00'];
const SYMS={hearts:'\\u2665',diamonds:'\\u2666',clubs:'\\u2663',spades:'\\u2660'};
function gid(){return 'xxxx-xxxx-xxxx'.replace(/x/g,()=>Math.floor(Math.random()*16).toString(16))}
const S={
  ws:null,pid:localStorage.getItem('d_pid')||gid(),token:localStorage.getItem('d_tok')||gid(),
  name:localStorage.getItem('d_name')||'',lang:localStorage.getItem('d_lang')||(navigator.language.startsWith('ru')?'ru':'en'),
  room:null,roomId:null,game:null,target:null,ready:false,isSpec:false,sound:true,
  durakN:parseInt(localStorage.getItem('d_dc')||'0'),streak:parseInt(localStorage.getItem('d_ws')||'0'),
  lastAct:0,goShown:false,transferMode:false
};
localStorage.setItem('d_pid',S.pid);localStorage.setItem('d_tok',S.token);

/* ── Utilities ── */
function t(k){return(I18N[S.lang]||I18N.en)[k]||k}
function showView(id){document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  const el=document.getElementById(id);if(el)el.classList.add('active')}
let toastTimer=0;
function showToast(msg,err){const el=document.getElementById('toast');el.textContent=msg;
  el.className='toast'+(err?' err':'')+' show';clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),2500)}
function updateI18n(){document.querySelectorAll('[data-i18n]').forEach(e=>{e.textContent=t(e.dataset.i18n)});
  document.querySelectorAll('[data-i18n-ph]').forEach(e=>{e.placeholder=t(e.dataset.i18nPh)})}
function throttle(){const n=Date.now();if(n-S.lastAct<250)return true;S.lastAct=n;return false}

/* ── Card Element ── */
function mkCard(suit,rank,fd){
  const el=document.createElement('div');
  if(fd){el.className='card face-down';return el}
  const sym=SYMS[suit]||'';
  el.className='card '+suit;
  el.innerHTML='<div class="corner tl"><div class="rk">'+rank+'</div><div class="st">'+sym+'</div></div>'+
    '<div class="center-suit">'+sym+'</div>'+
    '<div class="corner br"><div class="rk">'+rank+'</div><div class="st">'+sym+'</div></div>';
  return el;
}

/* ── WebSocket ── */
function connect(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  S.ws=new WebSocket(proto+'//'+location.host+'/ws');
  S.ws.onopen=()=>{send({type:'register',id:S.pid,token:S.token,name:S.name||'Player'})};
  S.ws.onmessage=e=>{try{handleMsg(JSON.parse(e.data))}catch(ex){console.error(ex)}};
  S.ws.onclose=()=>{setTimeout(connect,2000)};
  S.ws.onerror=()=>{};
}
function send(d){if(S.ws&&S.ws.readyState===WebSocket.OPEN)S.ws.send(JSON.stringify(d))}
function handleMsg(m){
  switch(m.type){
    case 'lobby':renderLobby(m.rooms);break;
    case 'room_joined':S.roomId=m.room_id;S.ready=false;S.goShown=false;if(!S.game)showView('v-room');break;
    case 'room_update':S.room=m;renderRoom(m);if(!S.game||S.game.phase==='game_over')showView('v-room');break;
    case 'game_state':if(m.phase!=='game_over')S.goShown=false;S.game=m;S.target=null;S.transferMode=false;renderGame();break;
    case 'error':showToast(t(m.msg)||m.msg,true);break;
    case 'emoji':showFEmoji(m);break;
    case 'left_room':S.room=null;S.roomId=null;S.game=null;S.isSpec=false;showView('v-lobby');
      send({type:'get_lobby'});break;
  }
}

/* ── Actions ── */
function setName(){const n=document.getElementById('nameInput').value.trim();if(!n)return;
  S.name=n;localStorage.setItem('d_name',n);showView('v-lobby');send({type:'register',id:S.pid,token:S.token,name:n})}
function createRoom(){const nm=document.getElementById('roomNameInput').value.trim();
  const mp=parseInt(document.getElementById('maxPSel').value);
  const tr=document.getElementById('transferCb').checked;
  send({type:'create_room',name:nm,max:mp,transfer:tr})}
function joinRoom(rid){send({type:'join_room',room_id:rid})}
function spectateRoom(rid){S.isSpec=true;send({type:'spectate',room_id:rid})}
function leaveRoom(){S.game=null;S.isSpec=false;send({type:'leave_room'})}
function toggleReady(){S.ready=!S.ready;send({type:'ready'})}
function copyCode(){if(S.roomId){navigator.clipboard.writeText(S.roomId).then(()=>showToast(t('copied'))).catch(()=>{})}}
function doPickUp(){if(throttle())return;send({type:'action',action:'pick_up'})}
function doDone(){if(throttle())return;send({type:'action',action:'done'})}
function doTransfer(){S.transferMode=true;showToast(t('select_card'))}
function playAgain(){S.game=null;S.goShown=false;document.getElementById('overlay').classList.add('hidden');send({type:'play_again'})}
function backToLobby(){document.getElementById('overlay').classList.add('hidden');leaveRoom()}
function toggleLang(){S.lang=S.lang==='en'?'ru':'en';localStorage.setItem('d_lang',S.lang);
  document.getElementById('langBtn').textContent=S.lang.toUpperCase();updateI18n();
  if(S.game)renderGame();else if(S.room)renderRoom(S.room)}
function toggleSound(){S.sound=!S.sound;document.getElementById('soundBtn').textContent=S.sound?'\\u{1F50A}':'\\u{1F507}'}
function toggleEmoji(){document.getElementById('emojiPanel').classList.toggle('hidden')}
function sendEmoji(e){send({type:'emoji',emoji:e});document.getElementById('emojiPanel').classList.add('hidden')}
function showFEmoji(m){const el=document.createElement('div');el.className='floating-emoji';el.textContent=m.emoji;
  el.style.left=30+Math.random()*60+'%';el.style.top='40%';document.getElementById('emojiFloat').appendChild(el);
  setTimeout(()=>el.remove(),2000)}

/* ── Lobby Render ── */
function renderLobby(rooms){
  const list=document.getElementById('roomList');
  if(!rooms||!rooms.length){list.innerHTML='<div class="no-rooms">'+t('no_rooms')+'</div>';return}
  let h='';
  rooms.forEach(r=>{
    const canJoin=r.state==='waiting'&&r.players<r.max;
    const stLbl=r.state==='waiting'?t('waiting'):t('in_progress');
    const trLbl=r.transfer?' [T]':'';
    h+='<div class="room-card"><div class="rc-info"><div class="rc-name">'+r.name+trLbl+'</div>'+
      '<div class="rc-meta">'+r.players+'/'+r.max+' &bull; '+stLbl+'</div>'+
      '<div class="rc-meta">'+r.names.join(', ')+'</div></div><div class="rc-btns">'+
      (canJoin?'<button class="btn btn-primary btn-sm" onclick="joinRoom(\''+r.id+'\')">'+t('join')+'</button>':'')+
      (r.state==='playing'?'<button class="btn btn-secondary btn-sm" onclick="spectateRoom(\''+r.id+'\')">'+t('watch')+'</button>':'')+
      '</div></div>';
  });
  list.innerHTML=h;
}

/* ── Room Render ── */
function renderRoom(m){
  if(!m||!m.players)return;
  document.getElementById('roomTitle').textContent=m.room?m.room.name:'';
  document.getElementById('roomCodeDisp').textContent=S.roomId||'';
  const slots=document.getElementById('playerSlots');
  let h='';
  const ps=m.players||[];
  const maxP=m.room?m.room.max:4;
  ps.forEach((p,i)=>{
    const isMe=p.id===S.pid;
    const cls='p-slot filled'+(p.ready?' ready':'');
    h+='<div class="'+cls+'"><div class="avatar" style="background:'+COLORS[i%4]+'">'+p.name.charAt(0).toUpperCase()+'</div>'+
      '<div class="sname">'+p.name+(isMe?' '+t('you'):'')+'</div>'+
      '<div style="font-size:11px;color:'+(p.ready?'var(--accent)':'var(--fg2)')+'">'+
      (p.ready?t('ready'):t('not_ready'))+'</div></div>';
  });
  for(let i=ps.length;i<maxP;i++){h+='<div class="p-slot"><div style="color:var(--fg2);font-size:24px">?</div></div>'}
  slots.innerHTML=h;
  const rb=document.getElementById('readyBtn');
  const me=ps.find(p=>p.id===S.pid);
  if(me){rb.textContent=me.ready?t('not_ready'):t('ready');rb.className='btn '+(me.ready?'btn-secondary':'btn-primary')}
}

/* ── Game Render ── */
function renderGame(){
  const gs=S.game;if(!gs)return;showView('v-game');
  renderDeck(gs);renderDiscard(gs);renderTable(gs);renderStatus(gs);
  if(gs.yi>=0){renderOpps(gs);renderHand(gs);renderActions(gs);renderPInfo(gs)}
  else{renderAllOpp(gs);document.getElementById('playerHand').innerHTML='';
    document.getElementById('actionBar').innerHTML='';
    document.getElementById('pInfo').innerHTML='<span class="spec-label">'+t('spectating')+'</span>'}
  processLog(gs);
  if(gs.phase==='game_over'&&!S.goShown){S.goShown=true;setTimeout(()=>showGameOver(gs),400)}
}

function renderOpps(gs){
  const area=document.getElementById('oppArea');area.innerHTML='';
  gs.ps.forEach((p,i)=>{if(i===gs.yi)return;area.appendChild(mkOppPanel(p,i,gs))});
}
function renderAllOpp(gs){
  const area=document.getElementById('oppArea');area.innerHTML='';
  gs.ps.forEach((p,i)=>{area.appendChild(mkOppPanel(p,i,gs))});
}
function mkOppPanel(p,i,gs){
  const panel=document.createElement('div');
  let cls='opp-panel';
  if(p.atk)cls+=' is-atk';if(p.def)cls+=' is-def';if(p.out)cls+=' is-out';
  const isActive=(p.atk&&!gs.tbl.some(tp=>!tp.d)&&gs.phase==='playing')||
                  (p.def&&gs.tbl.some(tp=>!tp.d)&&!gs.pu&&gs.phase==='playing');
  if(isActive)cls+=' active-glow';
  panel.className=cls;
  let av='<div class="avatar" style="background:'+COLORS[i%4]+'">'+p.n.charAt(0).toUpperCase()+'</div>';
  let info='<div class="opp-info">'+av+'<span class="opp-name">'+p.n+'</span></div>';
  let count='<div class="opp-count">'+p.cc+' '+t('cards_left')+'</div>';
  let cards='<div class="opp-cards">';
  for(let j=0;j<Math.min(p.cc,8);j++){cards+='<div class="opp-card"></div>'}
  cards+='</div>';
  panel.innerHTML=info+count+cards;
  return panel;
}

function renderDeck(gs){
  const area=document.getElementById('deckArea');area.innerHTML='';
  if(gs.dc>0){
    if(gs.tc){const tc=mkCard(gs.tc.s,gs.tc.r,false);tc.classList.add('trump-under');area.appendChild(tc)}
    const dk=document.createElement('div');dk.className='card face-down deck-stack';
    dk.innerHTML='<div class="deck-count">'+gs.dc+'</div>';area.appendChild(dk);
  } else {
    const em=document.createElement('div');em.className='deck-empty';em.textContent='\\u2205';area.appendChild(em);
  }
  const ti=document.getElementById('trumpInd');
  if(gs.ts){const sym=SYMS[gs.ts];ti.innerHTML=t('trump')+': <span class="'+gs.ts+'">'+sym+'</span>'}
}

function renderDiscard(gs){
  const area=document.getElementById('discardArea');area.innerHTML='';
  if(gs.disc>0){
    const cnt=Math.min(gs.disc,5);
    for(let i=0;i<cnt;i++){
      const d=document.createElement('div');d.className='card face-down disc-card';
      const rot=(Math.random()-.5)*25;const tx=(Math.random()-.5)*8;const ty=(Math.random()-.5)*8;
      d.style.cssText='transform:rotate('+rot+'deg) translate('+tx+'px,'+ty+'px);position:absolute;'+
        'width:calc(var(--card-w)*.7);height:calc(var(--card-h)*.7)';area.appendChild(d);
    }
    const cl=document.createElement('div');cl.className='disc-count';cl.textContent=gs.disc;area.appendChild(cl);
  }
}

function renderTable(gs){
  const area=document.getElementById('playArea');area.innerHTML='';
  gs.tbl.forEach((pair,idx)=>{
    const pe=document.createElement('div');pe.className='table-pair';
    pe.style.animationDelay=idx*0.05+'s';
    const ae=mkCard(pair.a.s,pair.a.r,false);ae.classList.add('atk-card');
    ae.dataset.key=pair.a.r+'_'+pair.a.s;
    if(!pair.d&&gs.yi===gs.di&&!gs.pu&&gs.acts&&gs.acts.includes('defend')){
      ae.classList.add('targetable');
      if(S.target===ae.dataset.key)ae.classList.add('sel-target');
      ae.addEventListener('click',()=>selectTarget(ae.dataset.key));
    }
    pe.appendChild(ae);
    if(pair.d){const de=mkCard(pair.d.s,pair.d.r,false);de.classList.add('def-card');pe.appendChild(de)}
    area.appendChild(pe);
  });
}

function renderHand(gs){
  const hand=document.getElementById('playerHand');hand.innerHTML='';
  const playable=new Set(gs.pc||[]);
  gs.hand.forEach((c,i)=>{
    const key=c.r+'_'+c.s;const el=mkCard(c.s,c.r,false);el.dataset.key=key;
    if(playable.has(key)){el.classList.add('playable');el.addEventListener('click',()=>onCardClick(key,c))}
    const n=gs.hand.length;const mid=(n-1)/2;const off=i-mid;
    el.style.zIndex=i;
    if(n>1&&window.innerWidth>500){
      el.style.transform='rotate('+(off*2)+'deg) translateY('+Math.abs(off)*2+'px)';
    }
    hand.appendChild(el);
  });
}

function renderActions(gs){
  const bar=document.getElementById('actionBar');bar.innerHTML='';
  const acts=gs.acts||[];
  if(acts.includes('pick_up')){const b=document.createElement('button');b.className='btn btn-danger btn-sm';
    b.textContent=t('pick_up');b.onclick=doPickUp;bar.appendChild(b)}
  if(acts.includes('done')){const b=document.createElement('button');b.className='btn btn-primary btn-sm';
    b.textContent=t('done');b.onclick=doDone;bar.appendChild(b)}
  if(acts.includes('transfer')){const b=document.createElement('button');b.className='btn btn-secondary btn-sm';
    b.textContent=t('transfer');b.onclick=doTransfer;bar.appendChild(b)}
}

function renderPInfo(gs){
  const info=document.getElementById('pInfo');
  const me=gs.ps[gs.yi];if(!me){info.innerHTML='';return}
  let role='';
  if(me.atk)role='<span class="role-badge role-atk">'+t('attacking')+'</span>';
  else if(me.def)role='<span class="role-badge role-def">'+t('defending')+'</span>';
  else if(gs.phase==='playing')role='<span class="role-badge" style="background:var(--info);color:#fff">'+t('throwing')+'</span>';
  info.innerHTML='<div class="avatar" style="background:'+COLORS[gs.yi%4]+';width:24px;height:24px;border-radius:50%;'+
    'display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#fff">'+
    me.n.charAt(0).toUpperCase()+'</div> <span>'+me.n+' '+t('you')+'</span> '+role+
    ' <span style="color:var(--fg2);font-size:12px">'+me.cc+' '+t('cards_left')+'</span>';
}

function renderStatus(gs){
  const bar=document.getElementById('statusBar');
  if(gs.phase==='game_over'){bar.innerHTML='<span style="color:var(--accent2)">'+t('game_over')+'</span>';return}
  if(gs.yi<0){bar.innerHTML='';return}
  const me=gs.ps[gs.yi];
  const hasTurn=(me.atk&&!gs.tbl.some(tp=>!tp.d))||(me.def&&gs.tbl.some(tp=>!tp.d)&&!gs.pu);
  if(hasTurn){bar.innerHTML='<span class="turn-text">'+t('your_turn')+'</span>'}
  else{bar.innerHTML=''}
}

function selectTarget(key){
  S.target=key;
  document.querySelectorAll('.atk-card').forEach(el=>{
    el.classList.toggle('sel-target',el.dataset.key===key);
  });
}

function onCardClick(key,card){
  if(throttle())return;
  const gs=S.game;if(!gs)return;
  const acts=gs.acts||[];
  if(gs.yi===gs.di&&!gs.pu){
    if(S.transferMode&&acts.includes('transfer')){
      send({type:'action',action:'transfer',card:key});S.transferMode=false;return;
    }
    if(acts.includes('defend')){
      const ub=gs.tbl.filter(tp=>!tp.d);
      if(ub.length===1){
        send({type:'action',action:'defend',card:key,target:ub[0].a.r+'_'+ub[0].a.s});return;
      }
      if(S.target){send({type:'action',action:'defend',card:key,target:S.target});S.target=null;return}
      showToast(t('select_target'));return;
    }
  } else {
    if(acts.includes('attack')){send({type:'action',action:'attack',card:key});return}
  }
}

function processLog(gs){
  const log=gs.log||[];
  for(const e of log){
    if(e.t==='def'){const mid=document.getElementById('midArea');
      mid.classList.add('shake');setTimeout(()=>mid.classList.remove('shake'),200)}
  }
}

/* ── Game Over ── */
function showGameOver(gs){
  const ov=document.getElementById('overlay');ov.classList.remove('hidden');ov.innerHTML='';
  const isDurak=gs.durak===S.pid;
  const isFinished=gs.fin&&gs.fin.includes(S.pid);
  const isFirstOut=gs.fin&&gs.fin[0]===S.pid;
  let html='<div class="go-content">';
  if(isDurak){
    S.durakN++;localStorage.setItem('d_dc',S.durakN);S.streak=0;localStorage.setItem('d_ws','0');
    html+='<div class="dunce-cap"></div>';
    html+='<div class="banner bounce-in" style="color:var(--danger)">'+t('durak')+'</div>';
    html+='<div class="durak-counter">'+t('times_fool')+': '+S.durakN+'</div>';
    html+='<div class="card-scatter" id="cardScatter"></div>';
  } else if(gs.durak===null){
    html+='<div class="banner bounce-in" style="color:var(--accent2)">'+t('draw_game')+'</div>';
  } else {
    if(isFirstOut){S.streak++;localStorage.setItem('d_ws',S.streak)}
    html+='<div class="crown">\\u{1F451}</div>';
    html+='<div class="banner bounce-in shimmer">'+t('winner')+'</div>';
    if(S.streak>=3){html+='<div class="fire-badge">'+t('on_fire')+' \\u{1F525}</div>'}
  }
  html+='</div><div class="go-btns">'+
    '<button class="btn btn-primary" onclick="playAgain()">'+t('play_again')+'</button>'+
    '<button class="btn btn-secondary" onclick="backToLobby()">'+t('back_to_lobby')+'</button></div>';
  ov.innerHTML=html;
  if(isDurak){triggerDurakAnim(ov)}
  if(isFirstOut&&gs.durak!==null){triggerWinAnim(ov)}
}

function triggerDurakAnim(ov){
  const sc=document.getElementById('cardScatter');
  if(sc){for(let i=0;i<15;i++){
    const c=document.createElement('div');c.className='card face-down scatter-card';
    c.style.cssText='width:30px;height:44px;--sx:'+(Math.random()-.5)*500+'px;--sy:'+
      (Math.random()-.5)*300+'px;--sr:'+(Math.random()-.5)*720+'deg;animation-delay:'+i*0.04+'s';
    sc.appendChild(c)}}
  for(let i=0;i<25;i++){
    const cf=document.createElement('div');cf.className='confetti';
    cf.style.cssText='left:'+Math.random()*100+'%;background:'+
      ['#e53935','#1e88e5','#43a047','#ff9800','#9c27b0'][Math.floor(Math.random()*5)]+
      ';animation-delay:'+Math.random()*1.5+'s;animation-duration:'+(1.5+Math.random()*2)+'s';
    ov.appendChild(cf)}
}
function triggerWinAnim(ov){
  for(let i=0;i<12;i++){
    const sp=document.createElement('div');
    sp.style.cssText='position:absolute;width:6px;height:6px;border-radius:50%;background:#ffd700;'+
      'left:50%;top:40%;pointer-events:none;animation:scatter .8s ease-out forwards;'+
      '--sx:'+(Math.random()-.5)*300+'px;--sy:'+(Math.random()-.5)*200+'px;--sr:0deg;animation-delay:'+i*0.05+'s';
    ov.appendChild(sp)}
}

/* ── Init ── */
function init(){
  updateI18n();
  document.getElementById('langBtn').textContent=S.lang.toUpperCase();
  if(S.name){showView('v-lobby');document.getElementById('nameInput').value=S.name}
  connect();
}
window.addEventListener('load',init);
</script>
</body>
</html>"""

app = FastAPI()
room_mgr = RoomManager()
conns: Dict[str, WebSocket] = {}
p_rooms: Dict[str, str] = {}
p_names: Dict[str, str] = {}
p_tokens: Dict[str, str] = {}
spectators: Dict[str, str] = {}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(content=HTML_PAGE)


async def broadcast_lobby() -> None:
    rooms = room_mgr.lobby()
    msg = json.dumps({"type": "lobby", "rooms": rooms})
    for pid, ws in list(conns.items()):
        if pid not in p_rooms and pid not in spectators:
            try:
                await ws.send_text(msg)
            except Exception:
                pass


async def broadcast_room(rid: str) -> None:
    room = room_mgr.get(rid)
    if not room:
        return
    msg = {
        "type": "room_update",
        "room": room.to_dict(),
        "players": [{"id": pid, "name": room.player_names.get(pid, "?"),
                      "ready": room.ready.get(pid, False)} for pid in room.player_ids],
    }
    text = json.dumps(msg)
    for pid in list(room.player_ids) + list(room.spectator_ids):
        if pid in conns:
            try:
                await conns[pid].send_text(text)
            except Exception:
                pass


async def broadcast_game(room: Room) -> None:
    if not room.game:
        return
    for pid in room.player_ids:
        if pid in conns:
            try:
                st = room.game.state_for(pid)
                await conns[pid].send_text(json.dumps(st))
            except Exception:
                pass
    for pid in room.spectator_ids:
        if pid in conns:
            try:
                st = room.game.state_for(pid, spectator=True)
                await conns[pid].send_text(json.dumps(st))
            except Exception:
                pass


async def broadcast_to_room(room: Room, msg: Dict[str, Any]) -> None:
    text = json.dumps(msg)
    for pid in list(room.player_ids) + list(room.spectator_ids):
        if pid in conns:
            try:
                await conns[pid].send_text(text)
            except Exception:
                pass


async def handle_msg(pid: str, ws: WebSocket, data: Dict[str, Any]) -> None:
    mt = data.get("type", "")

    if mt == "get_lobby":
        rooms = room_mgr.lobby()
        await ws.send_text(json.dumps({"type": "lobby", "rooms": rooms}))

    elif mt == "create_room":
        name = str(data.get("name", ""))[:20].strip()
        max_p = min(max(int(data.get("max", 2)), 2), 4)
        transfer = bool(data.get("transfer", False))
        room = room_mgr.create(name, max_p, transfer, pid, p_names.get(pid, "Player"))
        p_rooms[pid] = room.id
        await ws.send_text(json.dumps({"type": "room_joined", "room_id": room.id}))
        await broadcast_room(room.id)
        await broadcast_lobby()

    elif mt == "join_room":
        rid = str(data.get("room_id", "")).upper()
        room = room_mgr.get(rid)
        if not room:
            await ws.send_text(json.dumps({"type": "error", "msg": "Room not found"}))
            return
        if not room.add_player(pid, p_names.get(pid, "Player")):
            await ws.send_text(json.dumps({"type": "error", "msg": "full"}))
            return
        p_rooms[pid] = room.id
        await ws.send_text(json.dumps({"type": "room_joined", "room_id": room.id}))
        await broadcast_room(room.id)
        await broadcast_lobby()

    elif mt == "ready":
        rid = p_rooms.get(pid)
        if not rid:
            return
        room = room_mgr.get(rid)
        if not room or room.state != "waiting":
            return
        room.ready[pid] = not room.ready.get(pid, False)
        await broadcast_room(rid)
        if room.all_ready() and len(room.player_ids) >= 2:
            room.start_game()
            await broadcast_game(room)
            await broadcast_lobby()

    elif mt == "action":
        rid = p_rooms.get(pid)
        if not rid:
            return
        room = room_mgr.get(rid)
        if not room or not room.game:
            return
        action = str(data.get("action", ""))
        result = room.game.act(pid, action, data)
        if "error" in result:
            await ws.send_text(json.dumps({"type": "error", "msg": result["error"]}))
            return
        await broadcast_game(room)
        if room.game.phase == "game_over":
            await broadcast_lobby()

    elif mt == "leave_room":
        rid = p_rooms.pop(pid, None)
        if rid:
            room = room_mgr.get(rid)
            if room:
                if room.state == "waiting":
                    room.remove_player(pid)
                    if not room.player_ids:
                        room_mgr.remove(rid)
                    else:
                        await broadcast_room(rid)
                elif room.state == "playing" and room.game:
                    idx = room.game.pidx(pid)
                    if idx is not None:
                        p = room.game.players[idx]
                        if not p.is_out:
                            if idx == room.game.defender_idx and room.game.unbeaten():
                                room.game.act(pid, "pick_up", {})
                            elif pid not in room.game.passed:
                                room.game.act(pid, "done", {})
                            p.is_out = True
                            if p.id not in room.game.finished:
                                room.game.finished.append(p.id)
                            room.game._check_all_out()
                            await broadcast_game(room)
        sid = spectators.pop(pid, None)
        if sid:
            room = room_mgr.get(sid)
            if room:
                room.spectator_ids.discard(pid)
        await ws.send_text(json.dumps({"type": "left_room"}))
        await broadcast_lobby()

    elif mt == "spectate":
        rid = str(data.get("room_id", "")).upper()
        room = room_mgr.get(rid)
        if not room:
            await ws.send_text(json.dumps({"type": "error", "msg": "Room not found"}))
            return
        room.spectator_ids.add(pid)
        spectators[pid] = rid
        await ws.send_text(json.dumps({"type": "room_joined", "room_id": rid}))
        if room.game:
            st = room.game.state_for(pid, spectator=True)
            await ws.send_text(json.dumps(st))
        else:
            await broadcast_room(rid)

    elif mt == "emoji":
        rid = p_rooms.get(pid) or spectators.get(pid)
        if rid:
            room = room_mgr.get(rid)
            if room:
                emoji = str(data.get("emoji", ""))[:4]
                idx = -1
                if room.game:
                    ix = room.game.pidx(pid)
                    if ix is not None:
                        idx = ix
                await broadcast_to_room(room, {"type": "emoji", "emoji": emoji, "player_idx": idx})

    elif mt == "play_again":
        rid = p_rooms.get(pid)
        if not rid:
            return
        room = room_mgr.get(rid)
        if not room:
            return
        if room.game and room.game.phase == "game_over":
            room.state = "waiting"
            room.game = None
            alive = [p for p in room.player_ids if p in conns]
            room.player_ids = alive
            room.ready = {p: False for p in alive}
            await broadcast_room(rid)
            await broadcast_lobby()


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    pid: Optional[str] = None
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            if data.get("type") == "register":
                pid = str(data.get("id", ""))
                name = str(data.get("name", "Player"))[:12]
                if pid in conns:
                    try:
                        await conns[pid].close()
                    except Exception:
                        pass
                conns[pid] = websocket
                p_names[pid] = name
                p_tokens[pid] = str(data.get("token", ""))
                if pid in p_rooms:
                    rid = p_rooms[pid]
                    room = room_mgr.get(rid)
                    if room:
                        room.dc_times.pop(pid, None)
                        await websocket.send_text(json.dumps({"type": "room_joined", "room_id": rid}))
                        if room.game and room.state == "playing":
                            st = room.game.state_for(pid)
                            await websocket.send_text(json.dumps(st))
                        else:
                            await broadcast_room(rid)
                    else:
                        p_rooms.pop(pid, None)
                        await websocket.send_text(json.dumps({"type": "lobby", "rooms": room_mgr.lobby()}))
                else:
                    await websocket.send_text(json.dumps({"type": "lobby", "rooms": room_mgr.lobby()}))
                continue
            if pid:
                await handle_msg(pid, websocket, data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if pid:
            conns.pop(pid, None)
            rid = p_rooms.get(pid)
            if rid:
                room = room_mgr.get(rid)
                if room:
                    if room.state == "waiting":
                        room.remove_player(pid)
                        p_rooms.pop(pid, None)
                        if not room.player_ids:
                            room_mgr.remove(rid)
                        else:
                            try:
                                await broadcast_room(rid)
                            except Exception:
                                pass
                    else:
                        room.dc_times[pid] = time.time()
                try:
                    await broadcast_lobby()
                except Exception:
                    pass
            sid = spectators.pop(pid, None)
            if sid:
                room = room_mgr.get(sid)
                if room:
                    room.spectator_ids.discard(pid)


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(_cleanup_loop())


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(15)
        now = time.time()
        for room in list(room_mgr.rooms.values()):
            for pid in list(room.dc_times):
                if now - room.dc_times[pid] > 60:
                    del room.dc_times[pid]
                    if room.game and room.state == "playing":
                        idx = room.game.pidx(pid)
                        if idx is not None and not room.game.players[idx].is_out:
                            if idx == room.game.defender_idx and room.game.unbeaten():
                                room.game.act(pid, "pick_up", {})
                            elif pid not in room.game.passed:
                                room.game.act(pid, "done", {})
                            room.game.players[idx].is_out = True
                            if pid not in room.game.finished:
                                room.game.finished.append(pid)
                            room.game._check_all_out()
                            await broadcast_game(room)
                    if room.state == "waiting":
                        room.remove_player(pid)
                        p_rooms.pop(pid, None)
                        if not room.player_ids:
                            room_mgr.remove(room.id)
                        else:
                            await broadcast_room(room.id)
                    await broadcast_lobby()
        room_mgr.cleanup()

if __name__ == "__main__":
    import uvicorn
    print("\n  ♠ ♥ ♣ ♦  DURAK ONLINE  ♦ ♣ ♥ ♠")
    print("  Open http://localhost:8000 in your browser\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
