from __future__ import annotations

import re
import secrets
import tempfile
import json
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from PIL import Image, ImageDraw, ImageFont

# 插件根目录以及运行期生成图片的临时目录。
# 词库数据文件都跟随插件一起分发，不依赖外部服务或系统路径。
PLUGIN_ROOT = Path(__file__).resolve().parent
TEMP_DIR = PLUGIN_ROOT / "temp"
WORDS_PATH = PLUGIN_ROOT / "data" / "words_by_length.json"
CET_ANSWERS_PATH = PLUGIN_ROOT / "data" / "cet_answers_by_length.json"
TEMP_DIR.mkdir(exist_ok=True)

# `/wordle` 命令支持三种形式：
# 1. /wordle           -> 默认 5 位
# 2. /wordle 1-10      -> 指定长度
# 3. /wordle stop      -> 停止当前会话中的游戏
WORDLE_COMMAND_RE = re.compile(r"^\s*/wordle(?:\s+(?P<arg>stop|10|[1-9]))?\s*$", re.IGNORECASE)

# 颜色取自经典 Wordle 风格：
# empty   未填写
# absent  字母不存在
# present 字母存在但位置错误
# correct 字母和位置都正确
# 元组顺序为：(填充色, 边框色, 文字色)
TILE_COLORS = {
    "empty": ("#FFFFFF", "#D3D6DA", "#1A1A1B"),
    "absent": ("#787C7E", "#787C7E", "#FFFFFF"),
    "present": ("#C9B458", "#C9B458", "#FFFFFF"),
    "correct": ("#6AAA64", "#6AAA64", "#FFFFFF"),
}
INACTIVITY_TIMEOUT = timedelta(minutes=2)
ATTEMPTS_BY_LENGTH = {
    1: 3,
    2: 4,
    3: 5,
    4: 5,
    5: 6,
    6: 7,
    7: 8,
    8: 8,
    9: 9,
    10: 10,
}


@dataclass(slots=True)
class GuessAttempt:
    """一次猜测的最终记录。

    word:
        用户输入的原始单词，小写保存。
    states:
        每个字母对应的判定结果，长度与 word 相同。
    """

    word: str
    states: list[str]


@dataclass(slots=True)
class GameState:
    """单个群聊/私聊会话中的一局 Wordle 状态。"""

    answer: str
    word_length: int
    max_attempts: int = 6
    guesses: list[GuessAttempt] = field(default_factory=list)
    updated_at: datetime = field(default_factory=datetime.now)

    @property
    def won(self) -> bool:
        """最后一次猜测是否已经命中答案。"""
        return bool(self.guesses) and self.guesses[-1].word == self.answer

    @property
    def lost(self) -> bool:
        """在未猜中的前提下，是否已经用完全部次数。"""
        return not self.won and len(self.guesses) >= self.max_attempts


class WordRepository:
    """管理两个不同用途的词库。

    - `words_by_length.json`
      用于“验词”，也就是判断用户输入的英文单词是否合法。
    - `cet_answers_by_length.json`
      用于“出题”，答案只从四六级词汇里随机抽取。
    """

    def __init__(self) -> None:
        raw_words = json.loads(WORDS_PATH.read_text(encoding="utf-8"))
        raw_answers = json.loads(CET_ANSWERS_PATH.read_text(encoding="utf-8"))
        self._words_by_length = {int(length): words for length, words in raw_words.items()}
        self._answers_by_length = {int(length): words for length, words in raw_answers.items()}
        self._word_sets_by_length = {length: set(words) for length, words in self._words_by_length.items()}

    def pick_answer(self, length: int) -> str:
        """从指定长度的 CET 词表中随机抽一个答案。"""
        answers = self._answers_by_length.get(length, [])
        if not answers:
            raise ValueError(f"没有可用的 {length} 位 CET 单词可用于出题")
        return secrets.choice(answers)

    def is_valid_word(self, word: str) -> bool:
        """判断用户输入的单词是否在公开英文词表中。"""
        return word in self.get_word_set(len(word))

    def get_words(self, length: int) -> list[str]:
        """获取指定长度的公开英文词表。"""
        return self._words_by_length[length]

    def get_word_set(self, length: int) -> set[str]:
        """以 set 形式返回词表，便于高频 membership 查询。"""
        return self._word_sets_by_length[length]


class WordleRenderer:
    """负责把当前棋盘状态渲染成一张 Wordle 风格图片。

    这里故意只渲染棋盘本体，不渲染标题、提示信息或键盘，
    以保持和用户给出的经典截图一致。
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.tile_font = self._load_font(34)

    def render(self, game: GameState) -> str:
        """把当前游戏状态画成 PNG，并返回生成后的文件路径。"""

        # 根据单词长度动态调整格子大小。
        # 位数更长时缩小格子，避免图片宽度失控。
        tile_size = max(42, min(72, 360 // max(5, game.word_length)))
        tile_gap = 6
        board_width = game.word_length * tile_size + (game.word_length - 1) * tile_gap
        board_height = game.max_attempts * tile_size + (game.max_attempts - 1) * tile_gap
        canvas_width = board_width + 16
        canvas_height = board_height + 16

        image = Image.new("RGB", (canvas_width, canvas_height), "#FFFFFF")
        draw = ImageDraw.Draw(image)
        board_x = 8
        board_y = 8

        # 固定绘制 6 行（或配置后的 max_attempts 行）棋盘。
        # 已猜过的行显示颜色和字母，未猜过的行保持空白边框。
        for row_idx in range(game.max_attempts):
            attempt = game.guesses[row_idx] if row_idx < len(game.guesses) else None
            for col_idx in range(game.word_length):
                x0 = board_x + col_idx * (tile_size + tile_gap)
                y0 = board_y + row_idx * (tile_size + tile_gap)
                x1 = x0 + tile_size
                y1 = y0 + tile_size

                letter = ""
                state = "empty"
                if attempt is not None:
                    letter = attempt.word[col_idx].upper()
                    state = attempt.states[col_idx]

                fill_color, border_color, text_color = TILE_COLORS[state]
                draw.rounded_rectangle((x0, y0, x1, y1), radius=8, fill=fill_color, outline=border_color, width=2)
                if letter:
                    # 使用 anchor="mm" 以格子中心点作为对齐基准，
                    # 比手动计算 bbox 偏移更稳定，也更接近“视觉居中”。
                    center_x = x0 + tile_size / 2
                    center_y = y0 + tile_size / 2
                    draw.text((center_x, center_y), letter, fill=text_color, font=self.tile_font, anchor="mm")

        # 生成临时文件而不是覆盖固定文件名，避免并发会话时图片冲突。
        with tempfile.NamedTemporaryFile(dir=self.output_dir, prefix="wordle_", suffix=".png", delete=False) as tmp:
            image.save(tmp.name, format="PNG")
            return tmp.name

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """加载 Pillow 自带默认字体的可缩放版本。

        这样可以避免引用 `C:/Windows/Fonts/...` 之类的系统资源，
        保证插件在不同环境中的可移植性更好。
        """
        return ImageFont.load_default(size=size)


@register(
    "astrbot_plugin_wordle",
    "xuan_yuan",
    "Play Wordle in AstrBot and reply with Wordle-style images.",
    "0.1.0",
)
class WordlePlugin(Star):
    """AstrBot 插件入口。

    `self.games` 使用 `session_id -> GameState` 的映射来维护会话级状态，
    因而群聊和私聊都会各自拥有独立的一局游戏。
    """

    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None) -> None:
        """初始化插件。

        这里把 `config` 设计为可选参数，是为了兼容不同版本/不同加载路径的 AstrBot：
        - 新一些的调用方式会传入 `context, config`
        - 某些旧版本或特定加载分支只会传入 `context`
        """

        super().__init__(context)
        self.repository = WordRepository()
        self.renderer = WordleRenderer(TEMP_DIR)
        self.games: dict[str, GameState] = {}

        # AstrBotConfig 与普通 dict 都支持 `.get()`，这里统一按映射接口读取。
        config_mapping = config or {}
        configured_attempts = int(config_mapping.get("max_attempts", 6))
        self.max_attempts = max(3, min(configured_attempts, 10))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_wordle(self, event: AstrMessageEvent):
        """统一处理命令和猜词消息。

        处理顺序：
        1. 先识别 `/wordle` 指令
        2. 如果不是指令，再看当前会话是否有进行中的游戏
        3. 如果有游戏，则只接管“指定长度的纯英文消息”
        """
        raw_text = event.message_obj.message_str.strip()
        session_id = event.get_session_id()

        command_match = WORDLE_COMMAND_RE.match(raw_text)
        if command_match:
            result = await self._handle_command(event, command_match.group("arg"))
            yield result
            return

        game = self.games.get(session_id)
        if game is None:
            return

        if self._is_timed_out(game):
            self.games.pop(session_id, None)
            return

        guess = raw_text.lower()
        if not self._is_guess_message(guess, game.word_length):
            return

        result = await self._handle_guess(event, game, guess)
        yield result

    async def _handle_command(self, event: AstrMessageEvent, arg: str | None):
        """处理 `/wordle` 系列命令。"""
        session_id = event.get_session_id()
        if arg and arg.lower() == "stop":
            game = self.games.pop(session_id, None)
            if game is None:
                return self._build_text_result(event, "当前会话还没有正在进行的 Wordle。")
            return self._build_text_result(event, f"游戏已结束，答案是 {game.answer}。")

        word_length = 5 if arg is None else int(arg)
        existing_game = self.games.get(session_id)
        if existing_game is not None:
            if self._is_timed_out(existing_game):
                self.games.pop(session_id, None)

        # 新开局时直接覆盖当前会话里的旧局。
        game = GameState(
            answer=self.repository.pick_answer(word_length),
            word_length=word_length,
            max_attempts=self._resolve_max_attempts(word_length),
        )
        self.games[session_id] = game
        return self._build_text_result(
            event,
            f"新的 {word_length} 字母 Wordle 已开始，共 {game.max_attempts} 次机会。2 分钟无人接龙将自动结束。",
        )

    async def _handle_guess(self, event: AstrMessageEvent, game: GameState, guess: str):
        """处理用户在进行中游戏里的单次猜词。"""

        # 先做合法性校验，不合法则不推进棋盘。
        if not self.repository.is_valid_word(guess):
            game.updated_at = datetime.now()
            return self._build_text_result(event, f"{guess.upper()} 不在词库中，请继续输入有效英文单词。")

        # 合法单词才参与 Wordle 判定，并写入历史记录。
        states = self._evaluate_guess(guess, game.answer)
        game.guesses.append(GuessAttempt(word=guess, states=states))
        game.updated_at = datetime.now()

        if game.won:
            # 猜中后立即清理会话状态，避免后续消息继续被接管。
            self.games.pop(event.get_session_id(), None)
            return self._build_text_result(event, f"恭喜猜中 {game.answer}，共用了 {len(game.guesses)} 次。")

        if game.lost:
            # 失败同样要清理状态，允许用户重新开局。
            self.games.pop(event.get_session_id(), None)
            return self._build_text_result(event, f"很遗憾，次数用完了，答案是 {game.answer}。")

        image_path = self.renderer.render(game)
        return self._build_image_result(event, image_path)

    def _build_text_result(self, event: AstrMessageEvent, text: str):
        """构建纯文本回复。

        根据需求，开始、结束、错误提示都只返回文本。
        """
        result = event.plain_result(text)
        result.stop_event()
        return result

    def _build_image_result(self, event: AstrMessageEvent, image_path: str):
        """构建纯图片回复。

        根据需求，正常猜词后只返回棋盘图片，不附带文字。
        """
        chain = [Comp.Image.fromFileSystem(image_path)]
        result = event.chain_result(chain)
        result.stop_event()
        return result

    def _resolve_max_attempts(self, word_length: int) -> int:
        """根据单词长度返回对应机会数。"""
        return ATTEMPTS_BY_LENGTH.get(word_length, self.max_attempts)

    def _is_timed_out(self, game: GameState) -> bool:
        """判断一局游戏是否已经超过无人接龙超时时间。

        超时后只静默清理状态，不发送任何提示。
        """
        return datetime.now() - game.updated_at >= INACTIVITY_TIMEOUT

    def _is_guess_message(self, text: str, word_length: int) -> bool:
        """判断消息是否符合“当前局可接管的猜词格式”。

        只接管：
        - 长度刚好匹配
        - 纯 ASCII
        - 全部是英文字母
        """
        return len(text) == word_length and text.isascii() and text.isalpha()

    def _evaluate_guess(self, guess: str, answer: str) -> list[str]:
        """执行一轮标准 Wordle 判定。

        规则分两步：
        1. 第一轮先标记所有位置完全正确的字母 `correct`
        2. 第二轮再用“剩余可匹配字母计数”判断 `present`

        这样可以正确处理重复字母，例如：
        - 答案里某字母只出现 1 次
        - 猜词里该字母出现 2 次
        那么最多只会有一个位置被标成 `present/correct`
        """
        result = ["absent"] * len(guess)
        remaining: dict[str, int] = {}

        # 第一轮：先找出完全命中的位置。
        # 未命中的答案字母会统计进 remaining，留给第二轮做“错位存在”判断。
        for index, (guess_char, answer_char) in enumerate(zip(guess, answer)):
            if guess_char == answer_char:
                result[index] = "correct"
            else:
                remaining[answer_char] = remaining.get(answer_char, 0) + 1

        # 第二轮：仅对第一轮仍是 absent 的位置继续检查。
        # 如果该字母仍在 remaining 里有余额，则标记为 present 并消耗一个计数。
        for index, guess_char in enumerate(guess):
            if result[index] != "absent":
                continue
            if remaining.get(guess_char, 0) > 0:
                result[index] = "present"
                remaining[guess_char] -= 1

        return result
