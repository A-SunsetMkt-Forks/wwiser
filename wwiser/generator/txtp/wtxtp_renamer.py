import  re

# Renames .txtp to other names.
# Used to  simplify repetitive prefixes like "play_bgm (BGM_TYPE_MUSIC=BGM_TYPE_M01)" > play_bgm (MUSIC=M01)
# Contains a list loaded of rename stems, when applies to final txtp.
# Also deletes unwanted .txtp by using the <skip> flag.

class TxtpRenamer(object):
    SKIP_FLAG = '<skip>'

    def __init__(self):
        self._items = []
        self._skips = []
        self.skip = False

    def add(self, items):
        if not items:
            return
        for item in items:
            parts = item.split(":")
            if len(parts) != 2:
                continue

            text_in = parts[0]
            text_out = parts[1]
            regex = None
            if '*' in text_in:
                replaces = { '(':'\(', ')':'\)', '[':'\[', ']':'\]', '.':'\.', '*':'.*?' }
                regex_in = text_in
                for key, val in replaces.items():
                    regex_in = regex_in.replace(key, val)
                regex = re.compile(regex_in, re.IGNORECASE)
            else:
                regex = re.compile(re.escape(text_in), re.IGNORECASE)

            item = (text_in, text_out, regex)
            if text_out == self.SKIP_FLAG:
                self._skips.append(item)
            else:
                self._items.append(item)
        return

    def apply_renames(self, name):
        if not self._items and not self._skips:
            return name

        # base renames
        for text_in, text_out, regex in self._items:
            if regex:
                name = regex.sub(text_out, name)
            else:
                name = name.replace(text_in, text_out)

        # clean extra stuff after cleanup            
        replaces = { '(=':'(', '[=':'[', '=)':')', '=]':']', '()':'', '[]':'' }
        for key, val in replaces.items():
            name = name.replace(key, val)
        while '  ' in name:
            name = name.replace("  ", " ")

        name.strip()

        # special "skip this txtp if rename matches" flag (for variables), lasts until next call
        # at the end b/c it should go after cleanup (extra spaces) and final name
        self.skip = False

        for text_in, text_out, regex in self._skips:
            if regex and regex.match(name) or text_in in name:
                self.skip = True
                break

        return name