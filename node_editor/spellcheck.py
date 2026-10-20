"""Spell checking with Windows' own checker (the one Edge and Word use), through its COM API.

Languages are whatever spell-checking languages are installed in Windows (Settings > Time & language).
Indices it reports are UTF-16 positions, the same as Qt's text positions. If the API or comtypes is
unavailable, languages() is empty and nothing is checked.
"""

import ctypes
from ctypes import HRESULT, POINTER, byref, c_ulong, c_void_p, c_wchar_p, wstring_at

try:
    import comtypes
    import comtypes.client
    from comtypes import COMMETHOD, GUID, IUnknown
except ImportError:  # the app still runs without it
    comtypes = None

_ole32 = ctypes.windll.ole32
_ole32.CoTaskMemFree.argtypes = [c_void_p]  # 64-bit pointers
_ole32.CoTaskMemFree.restype = None


def _take_string(ptr):
    """Copy a string Windows allocated for us, then free it."""
    if not ptr:
        return ""
    text = wstring_at(ptr)
    _ole32.CoTaskMemFree(ptr)
    return text


if comtypes is not None:
    class IEnumString(IUnknown):
        _iid_ = GUID("{00000101-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Next", (["in"], c_ulong, "celt"), (["in"], POINTER(c_void_p), "rgelt"),
                      (["in"], POINTER(c_ulong), "fetched")),
            COMMETHOD([], HRESULT, "Skip", (["in"], c_ulong, "celt")),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "Clone", (["out"], POINTER(POINTER(IUnknown)), "enum")),
        ]

    class ISpellingError(IUnknown):
        _iid_ = GUID("{B7C82D61-FBE8-4B47-9B27-6C0D2E0DE0A3}")
        _methods_ = [
            COMMETHOD([], HRESULT, "get_StartIndex", (["out"], POINTER(c_ulong), "value")),
            COMMETHOD([], HRESULT, "get_Length", (["out"], POINTER(c_ulong), "value")),
            COMMETHOD([], HRESULT, "get_CorrectiveAction", (["out"], POINTER(c_ulong), "value")),
            COMMETHOD([], HRESULT, "get_Replacement", (["in"], POINTER(c_void_p), "value")),
        ]

    class IEnumSpellingError(IUnknown):
        _iid_ = GUID("{803E3BD4-2828-4410-8290-418D1D73C762}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Next", (["in"], POINTER(POINTER(ISpellingError)), "value")),
        ]

    class ISpellChecker(IUnknown):
        _iid_ = GUID("{B6FD0B71-E2BC-4653-8D05-F197E412770B}")
        _methods_ = [
            COMMETHOD([], HRESULT, "get_LanguageTag", (["in"], POINTER(c_void_p), "value")),
            COMMETHOD([], HRESULT, "Check", (["in"], c_wchar_p, "text"),
                      (["out"], POINTER(POINTER(IEnumSpellingError)), "errors")),
            COMMETHOD([], HRESULT, "Suggest", (["in"], c_wchar_p, "word"),
                      (["out"], POINTER(POINTER(IEnumString)), "suggestions")),
            COMMETHOD([], HRESULT, "Add", (["in"], c_wchar_p, "word")),
            COMMETHOD([], HRESULT, "Ignore", (["in"], c_wchar_p, "word")),
            # (later methods - AutoCorrect, options, events, ComprehensiveCheck - aren't used)
        ]

    class ISpellCheckerFactory(IUnknown):
        _iid_ = GUID("{8E018A9D-2415-4677-BF08-794EA61F94BB}")
        _methods_ = [
            COMMETHOD([], HRESULT, "get_SupportedLanguages", (["out"], POINTER(POINTER(IEnumString)), "value")),
            COMMETHOD([], HRESULT, "IsSupported", (["in"], c_wchar_p, "tag"), (["out"], POINTER(ctypes.c_int), "ok")),
            COMMETHOD([], HRESULT, "CreateSpellChecker", (["in"], c_wchar_p, "tag"),
                      (["out"], POINTER(POINTER(ISpellChecker)), "checker")),
        ]

    _CLSID_FACTORY = GUID("{7AB36653-1796-484B-BDFA-E74F1DB7C1DC}")

_factory = None
_checkers = {}
REPLACE, DELETE = 2, 3  # CORRECTIVE_ACTION values (1 = get suggestions)


def _get_factory():
    global _factory
    if _factory is None and comtypes is not None:
        try:
            comtypes.CoInitialize()
        except OSError:
            pass  # already initialised on this thread (Qt does it)
        try:
            _factory = comtypes.client.CreateObject(_CLSID_FACTORY, interface=ISpellCheckerFactory)
        except OSError:
            _factory = False  # no spell checking API on this system
    return _factory or None


def _strings(enum):
    out = []
    ptr, fetched = c_void_p(), c_ulong()
    while enum.Next(1, byref(ptr), byref(fetched)) == 0 and fetched.value:
        out.append(_take_string(ptr.value))
    return out


def languages():
    """Installed spell-checking languages, e.g. ['en-US', 'ru-RU', 'fr-FR']."""
    factory = _get_factory()
    if factory is None:
        return []
    try:
        return _strings(factory.get_SupportedLanguages())
    except OSError:
        return []


def _checker(language):
    if language not in _checkers:
        factory = _get_factory()
        try:
            _checkers[language] = factory.CreateSpellChecker(language) if factory else None
        except OSError:
            _checkers[language] = None
    return _checkers[language]


def check(language, text):
    """[(start, length, replacement or None)] - misspelled spans (UTF-16 positions); a replacement is given
    when Windows is sure of the fix (e.g. a repeated word to delete gives '')."""
    checker = _checker(language) if language and text.strip() else None
    if checker is None:
        return []
    out = []
    errors = checker.Check(text)
    err = POINTER(ISpellingError)()
    while errors.Next(byref(err)) == 0 and err:
        action = err.get_CorrectiveAction()
        replacement = None
        if action == REPLACE:
            ptr = c_void_p()
            err.get_Replacement(byref(ptr))
            replacement = _take_string(ptr.value)
        elif action == DELETE:
            replacement = ""
        out.append((err.get_StartIndex(), err.get_Length(), replacement))
        err = POINTER(ISpellingError)()
    return out


def suggest(language, word, limit=6):
    checker = _checker(language)
    return _strings(checker.Suggest(word))[:limit] if checker else []


def add(language, word):
    """Add to the user's Windows dictionary for that language (it persists)."""
    checker = _checker(language)
    if checker:
        checker.Add(word)


def ignore(language, word):
    """Stop flagging this word until the app closes."""
    checker = _checker(language)
    if checker:
        checker.Ignore(word)


def default_language():
    """Spell checking is on by default: American English when Windows has it, else its first language, else off."""
    langs = languages()
    return "en-US" if "en-US" in langs else (langs[0] if langs else "")


OPTIONS = [("", "Off")] + [(tag, tag) for tag in languages()]  # a Spell check field's choices
DEFAULT = default_language()
