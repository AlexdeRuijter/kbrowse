"""Unit tests for the parts of kbrowse that do not need a browser.
Run with: python3 -m unittest test_kbrowse"""

import unittest

import kbrowse as kb


class InputTests(unittest.TestCase):
    def feed(self, data):
        return kb.InputParser().feed(data)

    def test_keys(self):
        self.assertEqual([(k.name, k.mods) for k in self.feed(b"aG\r\x7f")],
                         [("a", 0), ("G", 0), ("Enter", 0), ("Backspace", 0)])
        self.assertEqual(self.feed(b"\x1b")[0].name, "Escape")  # lone Escape
        self.assertEqual(self.feed(b"\x1b[27u")[0].name, "Escape")  # kitty protocol
        key = self.feed(b"\x1b[100;5u")[0]
        self.assertEqual((key.name, key.mods), ("d", kb.Key.CTRL))
        key = self.feed(b"\x04")[0]
        self.assertEqual((key.name, key.mods), ("d", kb.Key.CTRL))
        key = self.feed(b"\x1b[1;5A")[0]
        self.assertEqual((key.name, key.mods), ("Up", kb.Key.CTRL))
        self.assertEqual(self.feed("é".encode())[0].name, "é")

    def test_split_sequence(self):
        parser = kb.InputParser()
        self.assertEqual(parser.feed(b"\x1b[<0;10"), [])
        mouse = parser.feed(b";20M")[0]
        self.assertEqual((mouse.kind, mouse.button, mouse.x, mouse.y), ("press", 0, 10, 20))

    def test_mouse_and_paste(self):
        events = self.feed(b"\x1b[<35;5;6M\x1b[<65;1;1M\x1b[<0;3;4m\x1b[200~a\nb\x1b[201~")
        self.assertEqual([e.kind for e in events[:3]], ["move", "wheel", "release"])
        self.assertEqual(events[3].text, "a\nb")


class KeyEventTests(unittest.TestCase):
    def test_text_and_special(self):
        down, up = kb.key_events(kb.Key("A"))
        self.assertEqual((down["type"], down["text"], down["code"], down["modifiers"]), ("keyDown", "A", "KeyA", 8))
        self.assertEqual(up["type"], "keyUp")
        down, _ = kb.key_events(kb.Key("?"))
        self.assertEqual((down["code"], down["modifiers"]), ("Slash", 8))
        down, _ = kb.key_events(kb.Key("Enter"))
        self.assertEqual(down["text"], "\r")
        down, _ = kb.key_events(kb.Key("a", kb.Key.CTRL))
        self.assertEqual((down["type"], down["modifiers"]), ("rawKeyDown", 2))
        self.assertEqual(kb.key_events(kb.Key("é")), "é")


class GraphicsTests(unittest.TestCase):
    def test_transmit_once_then_edit_in_place(self):
        out = []
        gfx = kb.Graphics(out.append)
        gfx.draw("A" * 5000, 10, 10, 8, 4)
        gfx.draw("B" * 100, 10, 10, 8, 4)
        first, second = out[0].decode(), out[1].decode()
        self.assertIn("a=T,f=100,t=d", first)
        self.assertEqual(first.count("\x1b_G"), 2)  # chunked
        self.assertNotIn("a=T", second)
        self.assertIn("a=f,r=1,X=1,f=100", second)
        gfx.draw("C" * 100, 10, 10, 9, 4)  # new placement size: transmit again
        self.assertIn("a=T", out[2].decode())


class CrxTests(unittest.TestCase):
    def test_crx3_header_is_stripped(self):
        import struct
        zipped = b"PK\x03\x04rest"
        crx = b"Cr24" + struct.pack("<II", 3, 4) + b"head" + zipped
        self.assertEqual(kb.crx_to_zip(crx), zipped)
        self.assertEqual(kb.crx_to_zip(zipped), zipped)
        with self.assertRaises(ValueError):
            kb.crx_to_zip(b"nope")


class PrintableTests(unittest.TestCase):
    def test_escape_sequences_are_neutralised(self):
        text = kb.printable("D\x1b]52;c;aGk=\x07E\x9b31m")
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertNotIn("\x9b", text)
        self.assertEqual(kb.printable("héllo wörld"), "héllo wörld")


class UrlTests(unittest.TestCase):
    def test_to_url(self):
        search = "https://duckduckgo.com/?q={}"
        self.assertEqual(kb.to_url("example.com", search), "https://example.com")
        self.assertEqual(kb.to_url("http://x.y/z", search), "http://x.y/z")
        self.assertEqual(kb.to_url("rust lang", search), "https://duckduckgo.com/?q=rust+lang")
        self.assertEqual(kb.to_url("localhost:8000", search), "http://localhost:8000")

    def test_hint_labels(self):
        self.assertEqual(kb.hint_labels(3), ["a", "s", "d"])
        self.assertEqual(len(set(kb.hint_labels(80))), 80)
        self.assertEqual(len(set(kb.hint_labels(300))), 300)


if __name__ == "__main__":
    unittest.main()
