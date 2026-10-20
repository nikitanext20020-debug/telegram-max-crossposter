import re

import pytest

from app.text import (
    TextEntity,
    append_signature,
    build_signature_html,
    clean_telegram_text,
    render_max_html,
)


def entity(entity_type, text, fragment, *, url=None, start=0):
    py_offset = text.index(fragment, start)
    prefix = text[:py_offset].encode("utf-16-le")
    body = fragment.encode("utf-16-le")
    return TextEntity(entity_type, len(prefix) // 2, len(body) // 2, url)


def test_preserves_supported_formatting_and_escapes_html():
    text = "Жирно и курсив <код>"
    clean = clean_telegram_text(
        text,
        [
            entity("bold", text, "Жирно"),
            entity("italic", text, "курсив"),
            entity("code", text, "<код>"),
        ],
    )

    assert render_max_html(clean) == ("<b>Жирно</b> и <i>курсив</i> <code>&lt;код&gt;</code>")


def test_uses_telegram_utf16_offsets_after_emoji():
    text = "😀 тест"
    clean = clean_telegram_text(text, [TextEntity("underline", 3, 4)])
    assert render_max_html(clean) == "😀 <u>тест</u>"


def test_removes_visible_urls_and_mentions_but_keeps_sentence():
    text = "Смотри https://bad.example/x и пиши @foreign_bot сейчас"
    clean = clean_telegram_text(
        text,
        [
            entity("url", text, "https://bad.example/x"),
            entity("mention", text, "@foreign_bot"),
        ],
    )

    assert render_max_html(clean) == "Смотри и пиши сейчас"


def test_removes_bare_telegram_and_max_links_without_entities():
    text = "Источники: t.me/foreign, max.ru/another и http://example.org/a."
    clean = clean_telegram_text(text, [])
    assert render_max_html(clean) == "Источники: , и ."


def test_hidden_link_in_sentence_keeps_plain_words():
    text = "Можно открыть описание здесь."
    clean = clean_telegram_text(
        text,
        [entity("text_link", text, "здесь", url="https://example.com")],
    )

    assert render_max_html(clean) == "Можно открыть описание здесь."


def test_removes_promotional_link_line_without_orphan_emoji():
    text = "Обычный текст\n👀 ХОТ КОНТЕНТ\n🔥 Подписаться: https://t.me/foreign"
    clean = clean_telegram_text(
        text,
        [entity("text_link", text, "ХОТ КОНТЕНТ", url="https://t.me/foreign")],
    )

    assert render_max_html(clean) == "Обычный текст"


def test_link_entity_inside_bold_does_not_break_formatting():
    text = "Важный сайт и текст"
    clean = clean_telegram_text(
        text,
        [
            entity("bold", text, text),
            entity("url", text, "сайт"),
        ],
    )

    assert render_max_html(clean) == "<b>Важный и текст</b>"


def test_buttons_are_not_rendered_or_added_to_text():
    clean = clean_telegram_text("Только пост", [], button_urls=["https://bad.example"])
    assert render_max_html(clean) == "Только пост"


def test_signature_is_safe_and_is_the_only_active_link():
    source = render_max_html(clean_telegram_text("Пост https://bad.example", []))
    signature = build_signature_html("НАШ ТГК", "https://t.me/webm4ik")
    messages = append_signature(source, signature)

    assert messages == ['Пост\n\n<a href="https://t.me/webm4ik">НАШ ТГК</a>']
    assert re.findall(r'href="([^"]+)"', messages[0]) == ["https://t.me/webm4ik"]


def test_signature_is_present_on_textless_and_every_split_post():
    signature = build_signature_html("НАШ ТГК", "https://t.me/webm4ik")

    assert append_signature("", signature) == ['<a href="https://t.me/webm4ik">НАШ ТГК</a>']
    chunks = append_signature("А" * 120, signature, limit=80)
    assert len(chunks) > 1
    assert all(chunk.endswith(signature) for chunk in chunks)
    assert "".join(chunk.removesuffix(signature) for chunk in chunks) == "А" * 120


@pytest.mark.parametrize(
    ("text", "url"),
    [
        ("", "https://t.me/webm4ik"),
        ("НАШ ТГК", "javascript:alert(1)"),
        ("НАШ ТГК", "ftp://example.com"),
    ],
)
def test_rejects_invalid_signature(text, url):
    with pytest.raises(ValueError):
        build_signature_html(text, url)


VPN_URL = "https://t.me/NosokVPNBot?start=partner_8235497168"


def test_keeps_exact_partner_vpn_link_and_removes_other_promo():
    text = "#аниме #animeedit\nЛучший VPN\nАНИМЕ В ТГ\nРеклама https://bad.example"
    clean = clean_telegram_text(
        text,
        [
            entity("text_link", text, "Лучший VPN", url=VPN_URL),
            entity("text_link", text, "АНИМЕ В ТГ", url="https://t.me/foreign"),
        ],
    )

    assert render_max_html(clean) == (f'#аниме #animeedit\n<a href="{VPN_URL}">Лучший VPN</a>')


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/NosokVPNBot?start=partner_999",
        "https://t.me/NosokVPNBot",
        VPN_URL + "&ad=1",
        VPN_URL + "#ad",
        VPN_URL + "0",
        VPN_URL + '" onclick="alert(1)',
        VPN_URL.replace("https://", "http://"),
        VPN_URL.replace("t.me/", "t.me.evil.example/"),
        VPN_URL.replace("t.me/", "t.me@evil.example/"),
        VPN_URL.replace("NosokVPNBot", "AnotherVPNBot"),
        VPN_URL.replace("partner_", "partner%5F"),
        "https://bad.example/?next=" + VPN_URL,
        "javascript:alert(1)",
        None,
    ],
)
def test_same_vpn_label_does_not_allow_other_urls(url):
    text = "Пост\nЛучший VPN"
    clean = clean_telegram_text(text, [entity("text_link", text, "Лучший VPN", url=url)])

    assert render_max_html(clean) == "Пост"


def test_partner_link_survives_emoji_formatting_and_html_escaping():
    text = '😀 Лучший <VPN> & "тест"'
    label = 'Лучший <VPN> & "тест"'
    clean = clean_telegram_text(
        text,
        [
            entity("text_link", text, label, url=VPN_URL),
            entity("bold", text, label),
        ],
    )

    assert render_max_html(clean) == (
        f'😀 <a href="{VPN_URL}"><b>Лучший &lt;VPN&gt; &amp; &quot;тест&quot;</b></a>'
    )


def test_vpn_exception_does_not_copy_buttons_or_raw_urls():
    text = "Пост\n" + VPN_URL
    clean = clean_telegram_text(text, button_urls=[VPN_URL, "https://bad.example"])

    assert render_max_html(clean) == "Пост"


def test_only_vpn_and_signature_are_active_after_splitting():
    text = "😀 " + "Лучший VPN " * 30
    clean = clean_telegram_text(text, [entity("text_link", text, text[2:], url=VPN_URL)])
    source = render_max_html(clean)
    signature = build_signature_html("НАШ ТГК", "https://t.me/webm4ik")
    chunks = append_signature(source, signature, limit=180)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 180
        assert chunk.endswith(signature)
        assert chunk.count("<a ") == chunk.count("</a>")
        assert set(re.findall(r'href="([^"]+)"', chunk)) <= {
            VPN_URL,
            "https://t.me/webm4ik",
        }
    assert VPN_URL in source
