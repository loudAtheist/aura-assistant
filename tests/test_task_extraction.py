import os
import sys
import types

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

dotenv_stub = types.ModuleType("dotenv")
dotenv_stub.load_dotenv = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", dotenv_stub)

telegram_stub = types.ModuleType("telegram")


class _DummyMarkup:
    def __init__(self, *args, **kwargs):
        pass


class _DummyButton:
    def __init__(self, *args, **kwargs):
        pass


def _dummy_reply(*args, **kwargs):
    return None


telegram_stub.Update = type("Update", (), {"message": types.SimpleNamespace(reply_text=_dummy_reply)})
telegram_stub.ReplyKeyboardMarkup = _DummyMarkup
telegram_stub.InlineKeyboardMarkup = _DummyMarkup
telegram_stub.InlineKeyboardButton = _DummyButton
sys.modules.setdefault("telegram", telegram_stub)

telegram_ext_stub = types.ModuleType("telegram.ext")


class _DummyBuilder:
    def __init__(self, *args, **kwargs):
        pass


class _DummyHandler:
    def __init__(self, *args, **kwargs):
        pass


telegram_ext_stub.ApplicationBuilder = _DummyBuilder
telegram_ext_stub.MessageHandler = _DummyHandler
telegram_ext_stub.CallbackQueryHandler = _DummyHandler
telegram_ext_stub.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
telegram_ext_stub.filters = types.SimpleNamespace(TEXT=None, COMMAND=None)
sys.modules.setdefault("telegram.ext", telegram_ext_stub)

speech_stub = types.ModuleType("speech_recognition")
speech_stub.Recognizer = type("Recognizer", (), {})
speech_stub.AudioFile = type("AudioFile", (), {})
sys.modules.setdefault("speech_recognition", speech_stub)


class _DummyAudioSegment:
    @staticmethod
    def from_ogg(*args, **kwargs):
        return types.SimpleNamespace(export=lambda *a, **kw: None)


pydub_stub = types.ModuleType("pydub")
pydub_stub.AudioSegment = _DummyAudioSegment
sys.modules.setdefault("pydub", pydub_stub)

openai_stub = types.ModuleType("openai")


class _DummyOpenAI:
    def __init__(self, *args, **kwargs):
        pass


openai_stub.OpenAI = _DummyOpenAI
sys.modules.setdefault("openai", openai_stub)

lev_stub = types.ModuleType("Levenshtein")
lev_stub.distance = lambda a, b: abs(len(a) - len(b))
sys.modules.setdefault("Levenshtein", lev_stub)

from main import extract_task_list_from_command  # noqa: E402


def test_extract_task_list_without_punctuation_items():
    command = "в покупки добавь хлеб молоко сыр"
    tasks = extract_task_list_from_command(command, "Покупки", "Купить хлеб")
    assert tasks == ["Купить хлеб", "Купить молоко", "Купить сыр"]


def test_extract_task_list_multiple_phrases_without_commas():
    command = "добавь позвонить маме забрать посылку оплатить интернет"
    tasks = extract_task_list_from_command(command, None, "Позвонить маме")
    assert tasks == [
        "Позвонить маме",
        "Забрать посылку",
        "Оплатить интернет",
    ]


def test_extract_task_list_single_entry_keeps_single_task():
    command = "добавь купить хлеб"
    tasks = extract_task_list_from_command(command, None, "Купить хлеб")
    assert tasks == []
