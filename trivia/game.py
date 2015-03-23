import asyncio
import time
import math
import re
import logging
from datetime import datetime

from .models import *


logger = logging.getLogger(__name__)


class TriviaGame(object):
    """
    The main trivia game.

    """
    STATE_IDLE = 'idle'
    STATE_QUESTION = 'question'
    STATE_WAITING = 'waiting'
    STATE_LOCKED = 'locked'

    ROUND_TIME = 45.0
    WAIT_TIME = 10.0
    WAIT_TIME_EXTRA = 7.0
    INACTIVITY_TIMEOUT = ROUND_TIME * 4

    STREAK_STEPS = 5
    HINT_TIMING = 10.0
    HINT_COOLDOWN = 2.5
    HINT_MAX = 3

    RE_START = re.compile(r'^!start', re.I)
    RE_HINT = re.compile(r'^!h(int)?', re.I)
    RE_NEXT = re.compile(r'^!n(ext)?', re.I)

    RE_ADMIN = re.compile(r'^!a(?:dmin)? (\S+) ?(.*?)$', re.I)

    def __init__(self, broadcast):
        self.state = self.STATE_IDLE
        self.broadcast = broadcast
        self.queue = asyncio.Queue()
        self.last_action = time.time()
        self.timeout = None
        self.timer_start = None
        self.round = None
        self.player_count = 0
        self._reset_hints()
        self._reset_streak()

    def get_round_info(self):
        elapsed_time = (time.time() - self.timer_start) if self.round else 0
        timer = ''

        if self.state == self.STATE_QUESTION:
            game = ('<p class="question-info">#{round.id}</p>'
                    '<p class="question-categories">{round.question.category_names}</p>'
                    '<p class="question">{round.question.question}</p>').format(round=self.round)

            if self.hints['current'] is not None:
                game += '<p class="question-hint">Hint: {}</p>'.format(self.hints['current'])

            timer = ('<div class="timer-bar" style="width:{width}%" data-time-left="{time_left}"></div>'
                     '<div class="timer-value"><span>{time_left:.2f}</span>s</div>').format(
                width=(self.ROUND_TIME - elapsed_time) / self.ROUND_TIME * 100.0,
                time_left=self.ROUND_TIME - elapsed_time,
            )

        elif self.state == self.STATE_WAITING:
            game = '<p class="question-info">#{round.id}</p>'.format(round=self.round)
            answer = self.round.question.primary_answer

            if self.round.solved:
                game += ('<p><b>{round.solver.name}</b> got '
                         '<b>{round.points}</b> points for answering in <b>{round.time_taken:.2f}s</b>:'
                         '<br>{round.question.question}</p>').format(round=self.round)
                game += '<p>Correct answer:<br><b>{}</b></p>'.format(answer)
            else:
                game += ('<p>{round.question.question}</p><p><b>Time\'s up!</b> '
                         'Nobody got the answer: <b>{answer}</b></p>').format(round=self.round, answer=answer)

            timer = ('<div class="timer-bar colorless" style="width:{width}%" data-time-left="{time_left}"></div>'
                     '<div class="timer-value">Next round in: <span>{time_left}</span>s</div>').format(
                width=(self.WAIT_TIME - elapsed_time) / self.WAIT_TIME * 100.0,
                time_left=self.WAIT_TIME - elapsed_time,
            )

        elif self.state == self.STATE_IDLE:
            game = '<p>Trivia is not running.</p><p>Say <kbd>!start</kbd> to begin a new round.</p>'

        elif self.state == self.STATE_LOCKED:
            game = '<p>Trivia is stopped.</p><p>Only an administrator can start it.</p>'

        return {
            'game': game,
            'timer': timer,
        }

    @asyncio.coroutine
    def run(self):
        asyncio.async(self.run_chat())

    @asyncio.coroutine
    def chat(self, player, text):
        yield from self.queue.put((player, text))

    @asyncio.coroutine
    def run_chat(self):
        """
        Monitor chat for commands and question answers.

        """
        while True:
            player, text = yield from self.queue.get()
            self.last_action = time.time()

            if self.RE_ADMIN.search(text) and player['permissions'] > 0:
                match = self.RE_ADMIN.match(text)
                admin = AdminCommand(self, player['id'])
                admin.run(match.group(1), *match.group(2).split())
                continue

            if self.state == self.STATE_QUESTION:
                if self.round.question.check_answer(text):
                    asyncio.get_event_loop().call_soon_threadsafe(self.timeout.cancel)
                    asyncio.async(self.round_solved(player))
                elif self.RE_HINT.search(text):
                    self.get_hint()

            if self.state == self.STATE_WAITING:
                if self.RE_NEXT.search(text) and self.has_streak(player):
                    self.next_round()

            if self.state == self.STATE_IDLE:
                if self.RE_START.search(text):
                    self.timeout = asyncio.async(self.delay_new_round(new_round=True))

    @asyncio.coroutine
    def round_solved(self, player):
        self.state = self.STATE_WAITING
        if self.streak['player_id'] == player['id']:
            self.streak['count'] += 1
            self.streak['player_name'] = player['name']
            if self.streak['count'] % self.STREAK_STEPS == 0:
                self.announce_streak(player['name'])
        else:
            if self.streak['count'] > self.STREAK_STEPS:
                self.announce_streak(player['name'], broken=True)
            self.streak = {
                'player_id': player['id'],
                'player_name': player['name'],
                'count': 1,
            }

        with db_session():
            player_db = get(p for p in Player if p.name == player['name'])
            played_round = Round[self.round.id]
            played_round.solved_by(
                player_db,
                self.ROUND_TIME,
                hints=self.hints['count'],
                streak=self.streak['count']
            )
            played_round.end_round()
            self.round = played_round
        asyncio.async(self.round_end())

    def next_round(self):
        """
        Skip to the next round.

        """
        if self.state == self.STATE_WAITING and self.timeout is not None:
            asyncio.get_event_loop().call_soon_threadsafe(self.timeout.cancel)
            asyncio.async(self.start_new_round())

    def stop_game(self, reason=None, lock=False):
        """
        Stop the game immediately no matter what.

        """
        if self.timeout is not None:
            asyncio.get_event_loop().call_soon_threadsafe(self.timeout.cancel)
        if lock:
            self.state = self.STATE_LOCKED
        else:
            self.state = self.STATE_IDLE

        asyncio.async(self.broadcast({
            'system': reason or "Stopping due to inactivity!",
        }))
        self.broadcast_info()

    @asyncio.coroutine
    def delay_new_round(self, new_round=False):
        self.state = self.STATE_WAITING
        wait = self.WAIT_TIME

        if new_round:
            wait = self.WAIT_TIME / 2
            self.round_start = datetime.now()
            self._reset_streak()
            asyncio.async(self.broadcast({
                'system': "New round starting in {:.2f}s!".format(wait),
            }))

        yield from asyncio.sleep(wait)

        if self.player_count < 1 or time.time() - self.last_action > self.INACTIVITY_TIMEOUT:
            self.stop_game()
        else:
            asyncio.async(self.start_new_round())

    @asyncio.coroutine
    def start_new_round(self):
        with db_session():
            try:
                new_round = Round.new(self.round_start)
            except IndexError:
                self.round_start = datetime.now()
                new_round = Round.new(self.round_start)
            commit()
            self.round = new_round
        self.timeout = asyncio.async(self.round_timeout())
        self.state = self.STATE_QUESTION
        self.timer_start = time.time()
        self._reset_hints()
        self.broadcast_info()

    @asyncio.coroutine
    def round_timeout(self):
        yield from asyncio.sleep(self.ROUND_TIME)
        with db_session():
            end_round = Round[self.round.id]
            end_round.end_round()
            self.round = end_round
        asyncio.async(self.round_end())

    @asyncio.coroutine
    def round_end(self):
        self.state = self.STATE_WAITING
        self.timer_start = time.time()
        self.broadcast_info()
        self.timeout = asyncio.async(self.delay_new_round())

    def broadcast_info(self):
        asyncio.async(self.broadcast({
            'setinfo': self.get_round_info(),
        }))

    def _reset_streak(self):
        self.streak = {
            'count': 0,
            'player_name': None,
            'player_id': None,
        }

    def has_streak(self, player):
        return self.streak['player_id'] == player['id'] and self.streak['count'] >= self.STREAK_STEPS

    def announce_streak(self, player_name, broken=False):
        streak = self.streak['count']
        if broken:
            asyncio.async(self.broadcast({
                'system': "{} broke {}'s streak of <b>{}</b>!".format(player_name, self.streak['player_name'], streak),
            }))
        else:
            info = "{} has reached a streak of <b>{}</b>!".format(player_name, streak)
            if streak == self.STREAK_STEPS:
                info += " You can skip to the next round with <kbd>!next</kbd>."
            asyncio.async(self.broadcast({
                'system': info,
            }))

    def _reset_hints(self):
        self.hints = {
            'count': 0,
            'current': None,
            'time': 0,
            'cooldown': 0,
        }

    def get_hint(self):
        if self.state != self.STATE_QUESTION or self.hints['count'] >= self.HINT_MAX:
            return

        now = time.time()
        elapsed_time = now - self.timer_start
        current_max_hints = math.ceil(elapsed_time / self.HINT_TIMING)

        if now - self.hints['time'] < self.HINT_COOLDOWN:
            return

        if current_max_hints > self.hints['count']:

            print("*** HINT #{} - after {:.2f}s (max {})".format(
                self.hints['count'] + 1,
                elapsed_time,
                current_max_hints
            ))

            self.hints['time'] = now
            self.hints['count'] += 1
            self.hints['current'] = self.round.question.get_hint(self.hints['count'])
            self.broadcast_info()


class AdminCommand(object):
    """
    Run an administrative command.

    """
    def __init__(self, game, player_id):
        self.game = game
        self.player_id = player_id

    def run(self, cmd, *args):
        if hasattr(self, cmd):
            with db_session():
                player = Player[self.player_id]
                if player.has_perm(cmd):
                    logger.info("{} executed: {}({!r})".format(player.name, cmd, args))
                    return getattr(self, cmd)(self, *args)
                else:
                    logger.warn("{} has no access to: {}".format(player, cmd))
        else:
            logger.info("Player #{} triggered unknown command: {}".format(self.player_id, cmd))

    def next(self, *args):
        self.game.next_round()

    def stop(self, *args):
        self.game.stop_game("Stopped by administrator.", lock='lock' in args)

    def unlock(self, *args):
        self.game.state = TriviaGame.STATE_IDLE
        self.game.broadcast_info()

    def start(self, *args):
        """If game is locked, only this will start it again."""
        self.game.timeout = asyncio.async(self.game.delay_new_round(new_round=True))
