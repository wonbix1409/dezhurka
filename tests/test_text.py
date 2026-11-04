from app.notify.text import escape


def test_slash_paths_do_not_become_bot_commands():
    # Telegram делает «/var» кликабельной командой — после слэша должен стоять word joiner
    assert escape("диск /var заполнен") == "диск /⁠var заполнен"
    assert escape("/usr/lib") == "/⁠usr/lib"
    assert escape('"/etc" (/opt)') == "&quot;/⁠etc&quot; (/⁠opt)"


def test_slash_inside_words_and_urls_untouched():
    assert escape("50/50, http://host/path") == "50/50, http://host/path"
    assert escape("a<b> & c") == "a&lt;b&gt; &amp; c"


def test_urls_in_details_become_whole_links():
    from app.notify.text import escape_with_links

    url = "http://localhost:9090/graph?g0.expr=up+%3D%3D+0&g0.tab=1"
    html = escape_with_links(f"node на /srv не отвечает.\n{url}.")
    assert html == (
        "node на /⁠srv не отвечает.\n"
        '<a href="http://localhost:9090/graph?g0.expr=up+%3D%3D+0&amp;g0.tab=1">🔗 localhost:9090</a>.'
    )
    assert escape_with_links("без ссылок <b>") == "без ссылок &lt;b&gt;"
