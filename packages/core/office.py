from __future__ import annotations

import csv
import datetime
import io
import zipfile
from typing import Any
from xml.etree import ElementTree

from packages.core.normalise import Normalised, UnsupportedFormat, tidy, title_from

# ---------------------------------------------------------------------------
# SLIDES AND SPREADSHEETS.
#
# Both are zips of XML, like .docx, so both are read with the standard library
# and nothing is added to the dependency list. What matters in each case is the
# unit a person would cite:
#
#   a deck      → the SLIDE. "It's on slide 12" is how people talk about decks,
#                 and it is the only landmark a slide file offers.
#   a workbook  → the SHEET, and inside it the ROW/COLUMN grid. A spreadsheet
#                 flattened into a run of values is the same failure that made
#                 PDF tables unreadable, so the grid is preserved as Markdown.
#
# Slides become `<!-- page N -->` sections, the same marker a PDF gets, so the
# page index and citations treat a deck exactly as they treat a document. That
# is a small lie in the marker's name and a large truth in behaviour: slide 4
# is page 4 of the deck, and the reader who clicks a citation lands where they
# expect.
# ---------------------------------------------------------------------------


def _open(data: bytes, filename: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormat(f"{filename} is not a readable Office file") from exc


class PptxParser:
    """PowerPoint decks, one section per slide.

    Read in slide order, which is the order the file numbers them — not the
    order the XML parts happen to be zipped in, which is arbitrary and would
    scramble a deck's argument.

    Speaker notes are included, marked as notes. They are frequently where the
    actual claim lives ("the 30% figure is pre-adjustment"), and a store that
    dropped them would answer confidently from a headline that the note
    qualifies.
    """

    extensions: tuple[str, ...] = (".pptx",)

    _A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    _CORE = "{http://purl.org/dc/elements/1.1/}"

    def _slide_number(self, name: str) -> int:
        digits = "".join(ch for ch in name.rsplit("/", 1)[-1] if ch.isdigit())
        return int(digits) if digits else 0

    def _text_of(self, xml: bytes) -> list[str]:
        """Every paragraph of visible text, in document order.

        Paragraph by paragraph rather than run by run: a single sentence is
        split across runs wherever formatting changes, so joining runs without
        respecting paragraphs turns a bulleted list into one long line.
        """
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            return []
        lines: list[str] = []
        for para in root.iter(f"{self._A}p"):
            parts = [node.text or "" for node in para.iter(f"{self._A}t")]
            line = "".join(parts).strip()
            if line:
                lines.append(line)
        return lines

    def parse(self, data: bytes, filename: str) -> Normalised:
        archive = _open(data, filename)
        slides = sorted(
            (n for n in archive.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
            key=self._slide_number,
        )
        if not slides:
            raise UnsupportedFormat(f"no slides found in {filename}")

        sections: list[str] = []
        for name in slides:
            number = self._slide_number(name)
            lines = self._text_of(archive.read(name))

            notes_name = f"ppt/notesSlides/notesSlide{number}.xml"
            notes = self._text_of(archive.read(notes_name)) if notes_name in archive.namelist() else []
            # The deck's own slide-number placeholder repeats the number back;
            # keeping it would put a bare "12" in the middle of the text.
            notes = [n for n in notes if n.strip() != str(number)]

            block = [f"<!-- page {number} -->"]
            if lines:
                # The first line of a slide is its title far more often than
                # not, and a heading is what the page index navigates by.
                block.append(f"# {lines[0]}")
                block.extend(lines[1:])
            if notes:
                block.append("")
                block.append("**Speaker notes:** " + " ".join(notes))
            if len(block) > 1:
                sections.append("\n".join(block))

        if not sections:
            raise UnsupportedFormat(f"no text in any slide of {filename}")

        body = tidy("\n\n---\n\n".join(sections))
        declared = ""
        try:
            core = ElementTree.fromstring(archive.read("docProps/core.xml"))
            found = core.find(f"{self._CORE}title")
            declared = (found.text or "").strip() if found is not None else ""
        except (KeyError, ElementTree.ParseError):
            declared = ""

        return Normalised(
            title=declared[:200] or title_from(body, filename),
            body=body,
            metadata={"format": "pptx", "slides": len(slides)},
        )


class XlsxParser:
    """Excel workbooks, one section per sheet, each sheet a Markdown table.

    The grid is the content. A spreadsheet read as a stream of values is the
    exact failure that made PDF tables useless — every number present and none
    of them attached to the label that gives it meaning — so rows and columns
    are preserved, and the first row is treated as the header it almost always
    is.

    Formulas are not evaluated: the cached VALUE Excel stored is read, which is
    what the author last saw on screen. A file saved without cached values
    reads as empty, and says so rather than inventing zeroes.
    """

    extensions: tuple[str, ...] = (".xlsx", ".xlsm")

    _NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    _REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    # Past this a sheet is a database export, not a document. Truncation is
    # said out loud in the text so nobody concludes the rest is absent.
    MAX_ROWS = 300
    MAX_COLS = 40

    # Excel keeps a date as a NUMBER — days since 1899-12-30 — and stores the
    # fact that it is a date in the cell's format, not in the cell. Ignore the
    # format and "2024-01-01" indexes as "45292", so the store answers a
    # question about a start date with a five-digit number. It is not even
    # wrong in a way a reader would catch.
    _EPOCH = datetime.date(1899, 12, 30)
    # The built-in format ids Excel reserves for dates and times.
    _DATE_FORMATS = frozenset(range(14, 23)) | frozenset(range(27, 37)) | frozenset(
        range(45, 48)
    ) | frozenset(range(50, 59))
    _PERCENT_FORMATS = frozenset({9, 10})

    def _cell_kinds(self, archive: zipfile.ZipFile) -> list[str]:
        """What each cell STYLE means: a date, a percentage, or a plain number.

        Read from styles.xml, which is the only place the answer lives. Custom
        formats are inspected for date letters rather than assumed — a
        workbook exported from anywhere but Excel numbers its own formats.
        """
        name = "xl/styles.xml"
        if name not in archive.namelist():
            return []
        try:
            root = ElementTree.fromstring(archive.read(name))
        except ElementTree.ParseError:
            return []

        custom: dict[int, str] = {}
        for fmt in root.iter(f"{self._NS}numFmt"):
            try:
                custom[int(fmt.get("numFmtId") or -1)] = (fmt.get("formatCode") or "").lower()
            except ValueError:
                continue

        kinds: list[str] = []
        cell_xfs = root.find(f"{self._NS}cellXfs")
        for xf in cell_xfs.iter(f"{self._NS}xf") if cell_xfs is not None else []:
            try:
                fmt_id = int(xf.get("numFmtId") or 0)
            except ValueError:
                fmt_id = 0
            code = custom.get(fmt_id, "")
            if fmt_id in self._DATE_FORMATS or (
                code and any(ch in code for ch in "ymd") and "0.00" not in code
            ):
                kinds.append("date")
            elif fmt_id in self._PERCENT_FORMATS or "%" in code:
                kinds.append("percent")
            else:
                kinds.append("plain")
        return kinds

    def _format(self, raw: str, kind: str) -> str:
        """One cell's value, as a person would read it off the screen."""
        if kind == "date":
            try:
                serial = float(raw)
            except ValueError:
                return raw
            try:
                stamp = self._EPOCH + datetime.timedelta(days=int(serial))
            except (OverflowError, ValueError):
                return raw
            fraction = serial - int(serial)
            if fraction > 0:
                minutes = round(fraction * 24 * 60)
                return f"{stamp.isoformat()} {minutes // 60:02d}:{minutes % 60:02d}"
            return stamp.isoformat()

        if kind == "percent":
            try:
                # Excel stores 78% as 0.78. Printing the stored number invites
                # every downstream answer to be off by two orders of magnitude.
                return f"{float(raw) * 100:g}%"
            except ValueError:
                return raw
        return raw

    def _shared_strings(self, archive: zipfile.ZipFile) -> list[str]:
        name = "xl/sharedStrings.xml"
        if name not in archive.namelist():
            return []
        try:
            root = ElementTree.fromstring(archive.read(name))
        except ElementTree.ParseError:
            return []
        out: list[str] = []
        for item in root.iter(f"{self._NS}si"):
            out.append("".join(t.text or "" for t in item.iter(f"{self._NS}t")))
        return out

    def _column(self, ref: str) -> int:
        """A1 -> 0, B1 -> 1, AA1 -> 26. The letters are base-26."""
        index = 0
        for ch in ref:
            if not ch.isalpha():
                break
            index = index * 26 + (ord(ch.upper()) - 64)
        return max(index - 1, 0)

    def _sheet_rows(
        self, xml: bytes, shared: list[str], styles: list[str] | None = None
    ) -> list[list[str]]:
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            return []

        styles = styles or []
        rows: list[list[str]] = []
        for row in root.iter(f"{self._NS}row"):
            cells: dict[int, str] = {}
            for cell in row.iter(f"{self._NS}c"):
                ref = cell.get("r") or ""
                kind = cell.get("t")
                value_node = cell.find(f"{self._NS}v")
                if kind == "inlineStr":
                    text = "".join(
                        t.text or "" for t in cell.iter(f"{self._NS}t")
                    )
                elif value_node is None:
                    text = ""
                elif kind == "s":
                    try:
                        text = shared[int(value_node.text or "0")]
                    except (ValueError, IndexError):
                        text = ""
                else:
                    text = value_node.text or ""
                    # A number only becomes a date or a percentage through its
                    # style, so the style is where the meaning is.
                    try:
                        style_index = int(cell.get("s") or -1)
                    except ValueError:
                        style_index = -1
                    if 0 <= style_index < len(styles):
                        text = self._format(text, styles[style_index])
                if text.strip():
                    cells[self._column(ref)] = text.strip()
            if cells:
                width = min(max(cells) + 1, self.MAX_COLS)
                rows.append([cells.get(i, "") for i in range(width)])
            if len(rows) >= self.MAX_ROWS:
                break
        return rows

    def _tidy_layout(self, rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
        """Strip the spacing a real spreadsheet is laid out with.

        Nobody starts typing in A1. A real workbook has a blank column down the
        left for margin, a title in B1, a subtitle in B2, and the actual column
        headings three rows down. Taken literally, the first row becomes the
        header — so the table's columns are named "" and "Excel Sample Data",
        and every value below sits under a heading that does not describe it.

        So: empty columns go, the rows above the header become a caption line,
        and the header is the first row that actually fills the table's width.
        """
        if not rows:
            return [], []

        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        keep = [i for i in range(width) if any(row[i].strip() for row in padded)]
        trimmed = [[row[i] for i in keep] for row in padded]
        if not trimmed:
            return [], []

        filled = [sum(1 for cell in row if cell.strip()) for row in trimmed]
        widest = max(filled)
        header_at = filled.index(widest)

        # Rows above the header are the sheet's own title block, kept as text
        # rather than thrown away: "Project Management Data" is what somebody
        # will search for.
        caption = [" ".join(c for c in row if c.strip()).strip() for row in trimmed[:header_at]]
        return [c for c in caption if c], trimmed[header_at:]

    def _as_markdown(self, rows: list[list[str]]) -> str:
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        header, *body = padded
        # A blank header would produce a table nothing can be read against, so
        # the columns get positional names instead.
        if not any(header):
            header = [f"Column {i + 1}" for i in range(width)]
        out = ["| " + " | ".join(header) + " |"]
        out.append("|" + "|".join(["---"] * width) + "|")
        for row in body:
            out.append("| " + " | ".join(row) + " |")
        return "\n".join(out)

    def parse(self, data: bytes, filename: str) -> Normalised:
        archive = _open(data, filename)
        shared = self._shared_strings(archive)

        # Sheet names live in the workbook part; the files themselves are
        # sheet1.xml, sheet2.xml. Reading the names matters because "Q3
        # forecast" is what someone will ask about, not "sheet2".
        names: list[str] = []
        try:
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            names = [
                (s.get("name") or f"Sheet {i + 1}")
                for i, s in enumerate(workbook.iter(f"{self._NS}sheet"))
            ]
        except (KeyError, ElementTree.ParseError):
            names = []

        sheets = sorted(
            (n for n in archive.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")),
            key=lambda n: int("".join(ch for ch in n.rsplit("/", 1)[-1] if ch.isdigit()) or 0),
        )
        if not sheets:
            raise UnsupportedFormat(f"no worksheets found in {filename}")

        styles = self._cell_kinds(archive)

        blocks: list[str] = []
        for index, name in enumerate(sheets):
            rows = self._sheet_rows(archive.read(name), shared, styles)
            if not rows:
                continue
            caption, rows = self._tidy_layout(rows)
            if not rows:
                continue
            title = names[index] if index < len(names) else f"Sheet {index + 1}"
            block = [f"## {title}", ""]
            if caption:
                block.extend(caption)
                block.append("")
            block.append(self._as_markdown(rows))
            if len(rows) >= self.MAX_ROWS:
                block.append("")
                block.append(f"*Only the first {self.MAX_ROWS} rows of this sheet were indexed.*")
            blocks.append("\n".join(block))

        if not blocks:
            raise UnsupportedFormat(
                f"no cell values in {filename} — a workbook saved without cached "
                "values has nothing to index"
            )

        body = "\n\n".join(blocks)
        return Normalised(
            title=title_from(body, filename),
            body=body,
            metadata={"format": "xlsx", "sheets": len(blocks)},
        )


class CsvParser:
    """Comma or tab separated data, as a Markdown table.

    Same reasoning as a workbook: the grid carries the meaning. The delimiter
    is sniffed rather than assumed, because a European export is semicolon
    separated and reading it as commas produces one column of gibberish.
    """

    extensions: tuple[str, ...] = (".csv", ".tsv")
    MAX_ROWS = 300

    def parse(self, data: bytes, filename: str) -> Normalised:
        text = data.decode("utf-8-sig", errors="replace")
        sample = text[:4096]
        try:
            dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

        rows = list(csv.reader(io.StringIO(text), dialect))
        rows = [r for r in rows if any(cell.strip() for cell in r)][: self.MAX_ROWS]
        if not rows:
            raise UnsupportedFormat(f"no rows in {filename}")

        width = max(len(r) for r in rows)
        padded = [[c.strip() for c in r] + [""] * (width - len(r)) for r in rows]
        header, *body = padded
        if not any(header):
            header = [f"Column {i + 1}" for i in range(width)]

        lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * width) + "|"]
        lines += ["| " + " | ".join(row) + " |" for row in body]

        return Normalised(
            # The FILENAME, not the first line. A CSV's first line is its
            # header row, so title_from produced documents called
            # "| Ticket | Client | Hours | Status |" — seen in the format
            # matrix, and it is what a person reads in every result list, every
            # citation and every deletion dialog. A file of pure data has no
            # title inside it; the name somebody gave the file is the best one
            # available.
            title=_title_from_filename(filename),
            body="\n".join(lines),
            metadata={"format": "csv", "rows": len(rows)},
        )


def _title_from_filename(filename: str) -> str:
    """`quarterly-spend_2026.csv` -> `Quarterly spend 2026`."""
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for extension in (".csv", ".tsv"):
        if stem.lower().endswith(extension):
            stem = stem[: -len(extension)]
            break
    words = " ".join(stem.replace("_", " ").replace("-", " ").split())
    return (words[:1].upper() + words[1:])[:200] or filename


__all__ = ["CsvParser", "PptxParser", "XlsxParser"]
