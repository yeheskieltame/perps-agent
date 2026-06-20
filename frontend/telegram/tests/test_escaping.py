"""User-controlled / backend-echoed text must be HTML-escaped before it goes into a
parse_mode=HTML message, or a grid name like '<b>x</b>' injects markup or breaks
Telegram rendering (a stray '<' makes Telegram reject the whole message)."""
from perpsbot import commands
from perpsbot.api import ApiError


def test_render_status_escapes_grid_name():
    out = commands.render_status([{
        "state": "RUNNING", "name": "<b>pwn</b>&", "instance_id": "i1",
        "realized_pnl": "0", "fill_count": 0,
    }])
    assert "<b>pwn</b>" not in out                     # raw markup neutralized
    assert "&lt;b&gt;pwn&lt;/b&gt;&amp;" in out        # escaped form present


def test_err_escapes_backend_detail():
    out = commands._err(ApiError(500, "<b>boom</b> & <i>x</i>"))
    assert "<b>boom</b>" not in out
    assert "&lt;b&gt;boom&lt;/b&gt; &amp; &lt;i&gt;x&lt;/i&gt;" in out
