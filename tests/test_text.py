from app.notify.text import escape


def test_slash_paths_do_not_become_bot_commands():
    # Telegram делает «/var» кликабельной командой — после слэша должен стоять word joiner
    assert escape("диск /var заполнен") == "диск /⁠var заполнен"
    assert escape("/usr/lib") == "/⁠usr/lib"
    assert escape('"/etc" (/opt)') == "&quot;/⁠etc&quot; (/⁠opt)"


def test_slash_inside_words_and_urls_untouched():
    assert escape("50/50, http://host/path") == "50/50, http://host/path"
    assert escape("a<b> & c") == "a&lt;b&gt; &amp; c"
