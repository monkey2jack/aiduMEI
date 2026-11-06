"""f0.7.6 redaction hardening — outside audit round 2 (R2-01..R2-07, R2-11).

Every POSITIVE case is a PoC string from the report (fake values only): it must be redacted and
the secret substring must be gone from the output.  Every NEGATIVE case is ordinary chat that
merely mentions a credential word: it must come back untouched.
"""
import pytest

from ducky.write_policy import redact_forbidden

HEX64 = "4f3c2b1a0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b"
SEED = "abandon ability able about above absent absorb abstract absurd abuse access accident"
TG = "7123456789:AAFakeBotTokenAbcdefghijklmnopqrstu12"
B64 = "ZmFrZXVzZXI6ZmFrZXBhc3MxMjM="

# (text, secret substring that must not survive)
POSITIVE = [
    # R2-01 lowercase value + natural separator
    ("我的wifi密码是 sunshine", "sunshine"),
    ("密码是abcdefgh", "abcdefgh"),
    ("密码为dragonfly", "dragonfly"),
    ("password is sunshine", "sunshine"),
    ("密码：sunshine", "sunshine"),
    # R2-02 placeholder whitelist no longer swallows real values
    ("password: HUNTERTWOSECRET", "HUNTERTWOSECRET"),
    ("PASSWORD=LETMEIN_NOW", "LETMEIN_NOW"),
    ("密码：ABCDEFGH", "ABCDEFGH"),
    ("password: summer_in_paris", "summer_in_paris"),
    ("password: $ecret2024Rex", "$ecret2024Rex"),
    ("password: <Hunter2024!x>", "Hunter2024!x"),
    ("password: Your-Dog-Rex-2024", "Your-Dog-Rex-2024"),
    ("password = Example#2024rex", "Example#2024rex"),
    # R2-03 camelCase / suffixed key names
    ('{"userPassword": "Hunter2024!x"}', "Hunter2024!x"),
    ("dbPassword=Hunter2024!x", "Hunter2024!x"),
    ('{"githubToken": "Tk9zQ7wE3rT5yU1i"}', "Tk9zQ7wE3rT5yU1i"),
    ('jwtSecret: "S3cr3tValue99"', "S3cr3tValue99"),
    ("openaiApiKey=Ab12Cd34Ef56Gh78", "Ab12Cd34Ef56Gh78"),
    ("DB_PASSWORD_PROD=Hunter2024!x", "Hunter2024!x"),
    ("SECRET_KEY_BASE=9f8e7d6c5b4a39281706", "9f8e7d6c5b4a39281706"),
    ("REDIS_PASSWORD_1=Hunter2024!x", "Hunter2024!x"),
    ("password2=Hunter2024!x", "Hunter2024!x"),
    # R2-04 quotes / no separator / verbs / "password for X is"
    ("password: \u201cHunter2024!x\u201d", "Hunter2024!x"),
    ("密码：「Hunter2024!x」", "Hunter2024!x"),
    ("我的密码Hunter2024!x别告诉别人", "Hunter2024!x"),
    ("密码改成了Hunter2024!x", "Hunter2024!x"),
    ("口令设置为Hunter2024!x", "Hunter2024!x"),
    ("密码——Hunter2024!x", "Hunter2024!x"),
    ("密码，Hunter2024!x", "Hunter2024!x"),
    ("password => Hunter2024!x", "Hunter2024!x"),
    ("the password for my gmail is Hunter2024!x", "Hunter2024!x"),
    # R2-05 value charset
    ("密码：老王爱吃饺子2024", "老王爱吃饺子2024"),
    ("password: Ab:cd!2024xyz", "Ab:cd!2024xyz"),
    ("password: Hu(nter)2024", "Hu(nter)2024"),
    ('password: "purple monkey dishwasher river"', "monkey dishwasher river"),
    ("password=\uff28unter\uff12024!x", "unter"),
    ("p\uff41s\uff53w\uff4fr\uff44=Hunter2024!x", "Hunter2024!x"),
    # R2-06 invisible and confusable characters
    ("pass\u202aword=Hunter2024!x", "Hunter2024!x"),
    ("sk-proj-\u2066abcdefghijklmnopqrstu12345", "abcdefghijklmnopqrstu12345"),
    ("pass\u034fword=Hunter2024!x", "Hunter2024!x"),
    ("pass\ufe0fword=Hunter2024!x", "Hunter2024!x"),
    ("pass\U000e0020word=Hunter2024!x", "Hunter2024!x"),
    ("pass\u3164word=Hunter2024!x", "Hunter2024!x"),
    ("p\u0430ssword: Hunter2024!x", "Hunter2024!x"),
    # R2-07 credential types not covered before
    (f"钱包私钥：{HEX64}", HEX64),
    (f"助记词：{SEED}", "absurd abuse access"),
    (f"bot token {TG}", "AAFakeBotToken"),
    ("stripe sk_test_FakeTestKey0123456789abcd", "FakeTestKey0123456789abcd"),
    ("DefaultEndpointsProtocol=https;AccountName=fakeacct;AccountKey=RmFrZUF6dXJlS2V5MDEyMzQ1Njc4OQ==;",
     "RmFrZUF6dXJlS2V5MDEyMzQ1Njc4OQ"),
    (f"Authorization: Basic {B64}", B64),
    ("Cookie: sessionid=fakesess0123456789abc; csrftoken=FakeCsrf987", "fakesess0123456789abc"),
    ("门禁PIN：4821", "4821"),
    ("银行卡密码：4821", "4821"),
    # Appendix B e2e table rows
    ('配置备忘 {"dbPassword": "Mk01Camel#Pw"}', "Mk01Camel"),
    ("DB_PASSWORD_PROD=Mk02Suffix#Pw", "Mk02Suffix"),
    ("password: MKTHREEALLCAPS", "MKTHREEALLCAPS"),
    ("password: mk_four_snake_case", "mk_four_snake_case"),
    ("wifi password: \u201cMk05Curly#Pw\u201d", "Mk05Curly"),
    ("我的银行密码Mk06NoSep#Pw", "Mk06NoSep"),
    ("the password for my gmail is Mk07ForIs#Pw", "Mk07ForIs"),
    ("家里门锁密码：马克七号老王家", "马克七号老王家"),
    ("pass\u202aword=Mk09Bidi#Pw", "Mk09Bidi"),
    ("password=\uff2dk10Full\uff12#Pw", "k10Full"),
    ("助记词：mkeleven abandon ability able about above absent absorb abstract absurd abuse access",
     "mkeleven"),
    ("token 7123456789:AAMk12TgFakeAbcdefghijklmnopqrstuvw", "AAMk12Tg"),
    ('{"adminPassword": "Mk21Auto#Pw"}', "Mk21Auto"),
    # a few more of our own
    ('{"x-api-key": "Fk3yAbCdEf123456"}', "Fk3yAbCdEf123456"),
    ("my pin is 4821", "4821"),
    ("api key: Fk3yAbCdEf123456", "Fk3yAbCdEf123456"),
    ("支付密码：135790", "135790"),
]

NEGATIVE = [
    "密码学很有意思",
    "token economy",
    "我忘了密码",
    "password policy requires 12 chars",
    "the secret to good coffee is fresh beans",
    # our own
    "我的密码是什么来着",
    "密码：已经改好了",
    "请把密码发给我",
    "我的银行卡密码提示是紫色长颈鹿",
    "password is too short",
    "the token is invalid",
    "password manager: Bitwarden",
    "max_tokens=4096",
    "TOKEN_LIMIT=4096",
    "Basic understanding of security",
    "token = <your token>",
    "password: YOUR_PASSWORD",
    "token: AIDUMEM_API_TOKEN",
    "api_key=${OPENAI_API_KEY}",
    "password: ********",
    "pin the tweet to the top",
    "the mnemonic is easy to remember when you have a good teacher and some time to practise daily",
    "我用 1Password 管理密码",
    "请输入密码：",
]


@pytest.mark.parametrize("text,secret", POSITIVE)
def test_poc_strings_are_redacted(text, secret):
    out, kinds = redact_forbidden(text)
    assert kinds and "[redacted:" in out, out
    assert secret not in out, out


@pytest.mark.parametrize("text", NEGATIVE)
def test_ordinary_chat_is_not_redacted(text):
    assert redact_forbidden(text) == (text, [])


def test_rest_of_sentence_and_invisible_chars_outside_the_span_survive():
    out, _ = redact_forbidden("记一下 pass\u202aword=Hunter2024!x 明天用")
    assert out.startswith("记一下 pass\u202aword=[redacted:credential]") and out.endswith(" 明天用")


def test_redaction_is_idempotent():
    once, _ = redact_forbidden('{"userPassword": "Hunter2024!x"} 密码是abcdefgh')
    assert redact_forbidden(once) == (once, [])


# R2-11: /evolve/feedback and /update run the same function; unit-level reuse of the PoC set.
FEEDBACK_CASES = [
    '{"userPassword": "FB02camel#Pw"}', "DB_PASSWORD_PROD=FB03suffix#Pw", "password: FBFOURALLCAPS",
    "密码：「FB05corner#Pw」", "我的密码FB06nosep#Pw",
]


@pytest.mark.parametrize("s", FEEDBACK_CASES)
def test_feedback_query_and_correction_are_redacted(s):
    """Mirror of routes_evolve.evolve_feedback: sanitize_memory_fields then redact_forbidden."""
    from ducky.scope_auth import sanitize_memory_fields
    q, c = sanitize_memory_fields(f"问句 {s}", f"改正 {s}")
    for v in (redact_forbidden(q)[0], redact_forbidden(c)[0]):
        assert "[redacted:" in v and not any(m in v for m in ("FB02", "FB03", "FBFOUR", "FB05", "FB06"))


@pytest.mark.parametrize("s", FEEDBACK_CASES + ['bob 配置 {"lockerPassword": "UPD02camel#Pw"}'])
def test_update_content_is_redacted(s):
    """/update persists through validate_and_sanitize_memory_content, which calls redact_forbidden."""
    from ducky.security.injection_guard import validate_and_sanitize_memory_content
    ok, cleaned, _ = validate_and_sanitize_memory_content(f"配置 {s}")
    assert ok and "[redacted:" in cleaned
    assert not any(m in cleaned for m in ("FB02", "FB03", "FBFOUR", "FB05", "FB06", "UPD02"))
