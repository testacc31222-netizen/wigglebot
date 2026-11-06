"""Durable Holiday Pass ledger. No network or Discord dependency."""
import copy
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from holiday_defaults import CHALLENGES, visible_challenge
from quest_rules import timed_play_quest

PHOENIX = timezone(timedelta(hours=-7))
SHOP = [
    ('gold500', '500 Gold', 150, 0), ('gold1000', '1,000 Gold', 275, 0),
    ('gold2500', '2,500 Gold', 600, 0), ('gold5000', '5,000 Gold', 1100, 0),
    ('helper', "Santa’s Little Helper", 350, 0), ('frostbite', 'Frostbite', 500, 0),
    ('present', 'Mystery Present', 400, 0), ('champion', 'Holiday Champion', 1000, 0),
    ('color', 'Custom Role Color · 7 days', 450, 0), ('shoutout', 'Staff Shoutout', 200, 0),
    ('vip', 'Event VIP', 550, 0), ('raffle', 'Holiday Raffle Ticket', 150, 0),
    ('autumn', 'Autumn Mystery Box', 500, 11), ('christmas', 'Christmas Mystery Box', 650, 12),
    ('royalty', 'Winter Royalty', 1500, 0),
]


class LedgerError(ValueError):
    pass


class HolidayStore:
    def __init__(self, path):
        self.path = Path(path)
        # Fail closed on corrupt data rather than silently resetting balances.
        self.data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def guild(self, gid):
        return self.data.get(str(gid), {'year': 2026, 'staff_roles': [], 'review_roles': [], 'forum': None, 'challenges': {cid: {'name': name, 'instructions': task, 'points': points, 'cadence': cadence, 'enabled': True} for cid, (name, task, points, cadence) in CHALLENGES.items()}, 'accounts': {}, 'claims': {}, 'orders': {}, 'proofs': {}})

    def change(self, gid, fn):
        proposed = copy.deepcopy(self.guild(gid))
        result = fn(proposed)
        all_data = {**self.data, str(gid): proposed}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(all_data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)
        self.data = all_data
        return result

    @staticmethod
    def active(g, now):
        local = now.astimezone(PHOENIX)
        return local.year == g['year'] and (local.month == 11 and local.day >= 2 or local.month == 12)

    @staticmethod
    def account(g, uid):
        return g.setdefault('accounts', {}).setdefault(str(uid), {'earned': 0, 'balance': 0})

    @staticmethod
    def board_key(now):
        local = now.astimezone(PHOENIX)
        return f'{local:%Y-%m-%d}:{local.hour // 2}'

    @classmethod
    def award(cls, g, uid, amount, now):
        a = cls.account(g, uid)
        a['earned'] += amount
        a['balance'] += amount
        key = cls.board_key(now)
        if g.get('snowflake_board', {}).get('window') != key:
            g['snowflake_board'] = {'window': key, 'scores': {}}
        scores = g['snowflake_board']['scores']
        scores[str(uid)] = scores.get(str(uid), 0) + amount

    def grant(self, gid, uid, amount, actor, reason, request_id, now):
        def op(g):
            if not isinstance(amount, int) or not 1 <= amount <= 100000 or not reason.strip():
                raise LedgerError('Enter 1–100,000 Snowflakes and a reason.')
            grants = g.setdefault('grants', {})
            if str(request_id) in grants:
                raise LedgerError('This grant has already been applied.')
            self.award(g, uid, amount, now)
            grants[str(request_id)] = {'user': uid, 'amount': amount, 'actor': actor, 'reason': reason, 'at': now.timestamp()}
        self.change(gid, op)

    def leaderboard(self, gid, now):
        board = self.guild(gid).get('snowflake_board', {})
        if board.get('window') != self.board_key(now):
            return []
        return sorted(board['scores'].items(), key=lambda pair: (-pair[1], int(pair[0])))

    @staticmethod
    def period(g, challenge, now):
        start = datetime(g['year'], 11, 2, tzinfo=PHOENIX)
        days = (now.astimezone(PHOENIX) - start).days
        return str(days if challenge['cadence'] == 'daily' else days // 7 if challenge['cadence'] == 'weekly' else 0)

    def create_claim(self, gid, uid, cid, now):
        def op(g):
            c = g['challenges'].get(cid)
            if not self.active(g, now) or not c or not c.get('enabled', True) or not visible_challenge(cid, c, g):
                raise LedgerError('This challenge is unavailable or the event is closed.')
            if c.get('expires') and now.timestamp() >= c['expires']:
                raise LedgerError('This secret mission has expired.')
            key = f'{uid}:{cid}:{self.period(g, c, now)}'
            if key in g['claims']:
                raise LedgerError('You already have a submission for this challenge/reset. Use its forum post.')
            g['claims'][key] = {'user': uid, 'challenge': cid, 'title': c['name'], 'instructions': c.get('instructions', ''), 'expires': c.get('expires'), 'points': c['points'], 'status': 'draft', 'created': now.timestamp(), 'thread': None}
            return key
        return self.change(gid, op)

    def submit(self, gid, key, hashes, now):
        def op(g):
            proof_hashes = list(hashes)
            c = g['claims'][key]
            if not self.active(g, now) or c['status'] != 'draft':
                raise LedgerError('This submission cannot be sent for review now.')
            if c.get('expires') and now.timestamp() >= c['expires']:
                raise LedgerError('The secret mission submission deadline has passed.')
            if timed_play_quest(c):
                if now.timestamp() < c['created'] + 30 * 60:
                    raise LedgerError('Your 30-minute session is still running. Finish playing before completing it.')
                proof_hashes = []
            elif not proof_hashes:
                raise LedgerError('Attach screenshot/video evidence to your forum post first.')
            if any(h in g['proofs'] for h in proof_hashes):
                raise LedgerError('This proof has already been submitted. Use different evidence.')
            for h in proof_hashes:
                g['proofs'][h] = key
            c.update(status='pending', hashes=proof_hashes, submitted=now.timestamp())
        self.change(gid, op)

    def review_claim(self, gid, key, reviewer, approved, reason='', now=None):
        def op(g):
            c = g['claims'][key]
            if c['user'] == reviewer:
                raise LedgerError('You cannot review your own submission.')
            if c['status'] != 'pending':
                raise LedgerError('This submission is not awaiting review.')
            c.update(status='approved' if approved else 'rejected', reviewer=reviewer, reason=reason)
            if approved:
                self.award(g, c['user'], c['points'], now or datetime.now(timezone.utc))
            return c
        return self.change(gid, op)

    def request_order(self, gid, uid, item_id, now):
        import uuid
        def op(g):
            item = next((i for i in SHOP if i[0] == item_id), None)
            if not self.active(g, now) or item is None or item[3] not in (0, now.astimezone(PHOENIX).month):
                raise LedgerError('This shop item is not available now.')
            if self.account(g, uid)['balance'] < item[2]:
                raise LedgerError('You do not have enough spendable Snowflakes.')
            if any(o['user'] == uid and o['status'] == 'pending' for o in g['orders'].values()):
                raise LedgerError('You already have a pending purchase. Wait for management to review it.')
            key = uuid.uuid4().hex[:12]
            g['orders'][key] = {'user': uid, 'item': item[1], 'cost': item[2], 'status': 'pending', 'thread': None, 'created': now.timestamp()}
            return key
        return self.change(gid, op)

    def review_order(self, gid, key, reviewer, action, now):
        def op(g):
            o = g['orders'][key]
            if o['user'] == reviewer:
                raise LedgerError('You cannot approve your own purchase.')
            if action == 'delivered':
                if o['status'] != 'approved':
                    raise LedgerError('Only approved purchases can be marked delivered.')
            else:
                if o['status'] != 'pending' or action not in ('approved', 'rejected'):
                    raise LedgerError('This purchase is no longer awaiting review.')
                if action == 'approved':
                    if not self.active(g, now):
                        raise LedgerError('The Holiday Shop is closed.')
                    a = self.account(g, o['user'])
                    if a['balance'] < o['cost']:
                        raise LedgerError('The member no longer has enough Snowflakes.')
                    a['balance'] -= o['cost']
            o.update(status=action, reviewer=reviewer)
            return o
        return self.change(gid, op)


