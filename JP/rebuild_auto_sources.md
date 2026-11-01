# Rebuilding the auto-captured catalog

`rebuild_auto_sources.py` redoes the auto-captured part of the catalog with the current
pipeline: meikiocr as the worker runs it, Sudachi analysis, the worker's auto-promotion,
and JMdict meanings.

It works on sources with `ingest_mode = 'auto_capture'` and `review_status = 'pending'`
(10,180 today). For each one it:

1. Re-runs OCR on the stored image (`MeikiOcrAdapter.extract_blocks`).
2. Replaces `raw_text` (`update_source_record`).
3. Replaces the analysis segments and candidates (`clear_source_analysis`,
   `store_source_analysis`).
4. Removes only the links that auto-promotion made, the ones with
   `notes = 'approved_from_source_analysis'`.
5. Promotes again (`auto_promote_source_terms`).

After the loop it fills missing term meanings through the JMdict path
(`auto_translate_term_meaning_pt`) and runs `rebuild-stats`.

Manual and reviewed sources are never selected. Every source is one transaction. The
`capture_jobs` rows are not touched, so the original EasyOCR text stays in
`capture_jobs.raw_text`.

```text
python JP/rebuild_auto_sources.py --db COPY.sqlite3 --jp-root JP [--limit N] [--sample N --seed S]
       [--dry-run] [--ignore-empty] [--ignore-missing-images] [--requeue-translations]
       [--skip-meanings] [--skip-rebuild-stats] [--fresh] [--allow-live-db]
```

- **Live database.** It refuses the live database unless you pass `--allow-live-db`. That
  means this checkout's `JP/data/lang_jp.sqlite3`, and the `lang_jp.sqlite3` that sits
  next to the captures it reads.
- **Resuming.** Finished sources go to `<db>.rebuild-progress.jsonl`. A rerun skips a
  source only while its text hash still matches that record, so a restored backup gets
  rebuilt again.
- **Dry run.** `--dry-run` runs everything, prints the old and new text, and rolls each
  source back.

Changes this needed elsewhere:

- `capture_worker.py`: the analysis and promotion steps are now shared helpers
  (`blocks_with_text`, `store_source_analysis`, `auto_promote_source_terms`). The worker
  behaves the same.
- `lang_jp.py`: `resolve_repo_local_path` takes a `root=`, and there is a new
  `AUTO_PROMOTION_NOTES` constant.
- `lang_jp.py` and `schema.sql`: one new index (see the speed section). It is the only
  schema change.

## Sample run, 2026-10-21

**Setup**

- **Database:** a copy of `JP/data/lang_jp.sqlite3` (sha256 `eeff7e52…`). The live file
  had the same hash and modification times after all the runs.
- **Sample:** 200 sources drawn at random (`--sample 200 --seed 1`) from the 10,180
  pending auto sources. They span 2026-03-29 to 2026-07-08.
- **Inputs:** images from `JP/data/captures` (3840x2160, two thirds WebP q45) and JMdict
  from `JP/data/jmdict.sqlite3`.
- **Runs:** three on fresh copies, with identical OCR output each time. The numbers below
  are from the last run, which used `--ignore-empty --ignore-missing-images`.

### Outcome

| status | sources | what happened |
|---|---:|---|
| rebuilt | 121 | new text, analysis and links |
| no_text | 69 | meikiocr found no Japanese text. Marked `ignored` by `--ignore-empty` |
| missing_image | 10 | image file is gone. Links dropped and marked `ignored` by `--ignore-missing-images` |

The 69 empty sources are desktop screenshots with no game on screen: a browser, chat,
Steam. EasyOCR read tiny UI text in them as 川, 日り, 口田, or picked up the 日本語 and 勉強
bookmark labels. That is where 日本語, 勉強 and 語 in the global top 30 come from. I opened
several of them and none had game text.

The missing images are all from 2026-04-08: 392 sources in the full population (ids
7000–7392). Their folder exists but these files do not.

### Speed

| | per image |
|---|---|
| OCR (meikiocr, CPU, 4K capture) | 0.45 s mean, 0.50 s median. 0.70 s in an earlier run with a cold file cache |
| DB work (analysis, promotion, stats) | 0.05 s |
| total | 0.51 s mean, 0.69 s p90 |
| `rebuild-stats` at the end | 469 s (one pass over the whole catalog) |

**Projected full run:** 10,180 × 0.5–0.75 s is 1.4–2.1 h, plus about 8 min of
`rebuild-stats`. The meaning step needs Google on top of that (see below). It costs about
0.5 s per term and runs once per term, not once per source.

Two fixes came out of the first run, which took 2.84 s per image:

- **Missing index.** `clear_source_analysis` alone took 2.06 s per source. Each deleted
  candidate fires `parent_candidate_id … ON DELETE SET NULL`, and that column had no
  index, so SQLite scanned all ~210k candidates for every deleted row. The new
  `idx_source_analysis_candidates_parent_candidate_id` brings this to milliseconds.
  `db_connect` creates the index the first time it opens the database, which takes about
  a second.
- **Meaning lookups during promotion.** Promotion called Google for every new term as it
  was created, about 0.5 s each while Google answered 429. The rebuild now defers those
  lookups to the meaning step after the loop.

### Top 30 terms by priority

Rebuilding 200 of 10,180 sources barely moves the global list: 川 went from 12,835.5 to
12,572.5. The table compares what the 200 sample sources contribute. It uses the
catalog's formula (occurrences + 3 × sources + 0.5 × forms) restricted to those sources,
before and after.

| # | before (sample) | score / sources | after (sample) | score / sources |
|---:|---|---|---|---|
| 1 | 川 カワ | 263.5 / 51 | する シ | 61.5 / 14 |
| 2 | 日 ヒ | 201.5 / 50 | ゲーム | 58.5 / 14 |
| 3 | 一 イチ | 62.5 / 14 | 言語 ゲンゴ | 56.5 / 14 |
| 4 | 山 ヤマ | 60.5 / 15 | 2 ニ | 52.5 / 13 |
| 5 | 口 クチ | 57.5 / 14 | ます | 50.5 / 12 |
| 6 | する シ | 54.5 / 12 | する スル | 45.5 / 11 |
| 7 | s | 53 / 13 | から | 41.5 / 10 |
| 8 | ゲーム | 52.5 / 13 | れる | 41.5 / 10 |
| 9 | 言語 ゲンゴ | 52.5 / 13 | 早送り ハヤオクリ | 40.5 / 10 |
| 10 | 町 マチ | 48.5 / 12 | 詳細 ショウサイ | 37.5 / 9 |
| 11 | j | 45 / 11 | ラダトーム | 36.5 / 9 |
| 12 | gamegengo | 44.5 / 11 | ログ | 36.5 / 9 |
| 13 | 1 イチ | 42.5 / 10 | です | 35.5 / 8 |
| 14 | れる | 41.5 / 10 | x | 32.5 / 8 |
| 15 | ラダトーム | 41.5 / 10 | いる | 32.5 / 7 |
| 16 | オート | 40.5 / 10 | オート | 32.5 / 8 |
| 17 | f | 36.5 / 9 | 勇者 ユウシャ | 30.5 / 7 |
| 18 | じり | 36.5 / 9 | たい | 29.5 / 7 |
| 19 | する スル | 36.5 / 9 | もの | 28.5 / 7 |
| 20 | 川山 カワヤマ | 36.5 / 9 | 1 イチ | 27.5 / 6 |
| 21 | いる | 35.5 / 8 | ロト | 26.5 / 6 |
| 22 | t トン | 32.5 / 8 | こと | 25.5 / 6 |
| 23 | から | 32.5 / 8 | せる | 25.5 / 6 |
| 24 | よう | 32.5 / 7 | 攻撃 コウゲキ | 25.5 / 5 |
| 25 | コー | 32.5 / 8 | する サ | 24.5 / 6 |
| 26 | です | 31.5 / 7 | カード | 24.5 / 5 |
| 27 | 勇者 ユウシャ | 30.5 / 7 | バトル | 24.5 / 6 |
| 28 | c | 29 / 7 | 旅 タビ | 24.5 / 6 |
| 29 | Local ローカル | 28.5 / 7 | 隊長 タイチョウ | 24.5 / 6 |
| 30 | ます | 28.5 / 7 | よう | 23.5 / 5 |

川, 日, 一, 山, 口, 町, 川山, s, j, f, c, gamegengo and Local are gone. In their place are
game words: 早送り, 詳細, ログ, 攻撃, バトル, 旅, 隊長.

What stays is not OCR junk but still not study material:

- **ゲーム and 言語** come from a "GameGengo ゲーム言語" window title that meikiocr reads
  correctly.
- **Numbers and Latin letters** (2, 1, x) are Sudachi tokens.

For reference, the global top 30 before the rebuild: 川 12,835.5 · 日 8,084.5 · 一 2,894.5 ·
山 2,585.5 · 口 2,535.5 · 町 · する(シ) · 語 · 日本 · 日本語 · よう · ゲーム · 言語 · ない · s ·
する(スル) · 1 · せい · もの · 勉強 · れる · gamegengo · オート · j · から · ラダトーム · f · ます ·
じり · カード (1,181.5).

### Links and terms

- **Links:** the sample's source_terms went from 1,782 rows to 1,525. Of the 1,781
  distinct (source, term) pairs, 895 were kept and 886 dropped, and 630 new pairs were
  added.
- **New terms:** 138. Examples: 討伐, 陛下, 弱点, 洞窟, 選択, 盾. Also some junk:
  `2026040412`, `ニニニニ…`.
- **Terms left with no sources in the sample run: 42.** 37 of them have flashcards.
  Examples: dagnose, stag, melhor, caminho, CHECK, DLC, 小1, マリアルワ, ブレイジングフ.
  - **Full-run estimate: about 3,300.** This is a rough model. 6,818 terms are linked
    only from pending auto sources, so that is the upper bound. 2,500 of those appear in
    a single source, and only 9% of single-source links survived in the sample.
  - The rebuild does not delete these terms. Their stats go to zero, so they sink to the
    bottom of the priority list. Their flashcards and Anki notes stay.
- **Meanings: 0 filled.** Google's `translate_a/single` endpoint answered **HTTP 429 Too
  Many Requests** throughout the session. The JMdict lookup itself works (川 → "river,
  stream"); only the EN→PT step failed. The meaning step noticed after 20 empty results,
  checked the translator, and stopped. Terms without a meaning went from 2,272 to 2,410,
  the 138 new terms included.

### Before and after, 10 examples

**#353 Dragon Quest intro: much better.**

```text
before: 闇の覇者 / 苦しむ人々を救うため 勇者口トの血を引くあなたは 竜王計供の旅に出る / 伝説の勇者が大魔王を倒し、 /
        竜王によって再び魔物に覆い尽くされてしまうc / 平和がもたらされたアレフガルドの地は
after:  ドラゴンクエスト! / 伝説の勇者が大魔王を倒し、平和がもたらされたアレフガルドの地は、 /
        闇の覇者・竜王によって再び魔物に覆い尽くされてしまう。 /
        苦しむ人々を救うため、勇者ロトの血を引くあなたは、竜王討伐の旅に出る―」 / 決定 オプション
```

**#622 Dialogue: the furigana lines are gone.**

```text
before: ラダトーム王 / そなたは / 本当に自分が / ほんとう / じぶん / 勇者 / ゆうしゃ / ロトの血をひく者だと / もの / 信じて / しん / おるのか? / 口Local
after:  ラダトーム王 / そなたは本当に自分が / 勇者ロトの血をひく者だと / 信じておるのか?
```

**#1853 Church menu.**

```text
before: おいのゆをする / どくのちりよう / のろいをとく / やめる / ロLocal / シスター / ここは / 神にみちびかれし / かみ / 迷える小羊たちの / … / こ用でしよう? / よう
after:  おいのりをする / どくのちりょう / のろいをとく / やめる / シスター / ここは神にみちびかれし /
        迷える小羊たちのおとずれる場所。 / わが教会にどんな、ご用でしょう?
```

**#5665 Card screen.**

```text
before: カード詩細 / しょうさい / 閉じる / 日り
after:  カード詳細 閉じる
```

**#4397 Visual novel line: the name is right now.**

```text
before: コバセシ / ホントは休ませてあげたいんだけど: / オート
after:  コバヤシ / いやー、遠くから大変だったでしょ…… / ホントは休ませてあげたいんだけど… 早送り / ・ログ / オート
```

**#6190 Story page: cleaner, but meikiocr drops some characters.** 空 is missing three
times, along with 近 and ろ.

```text
before: 川 / … 空を見るのが大好きて 「どうして空は青いの?」 / とか 「どうして雲は動くの?」 …
after:  … 知りたがっていました。を見るのが大好きで、どうしてはいの?」とか、どうして雲は動くの?」 …
```

**#7664 Desktop capture: correctly empty now.**

```text
before: 川 / 川山 / 山 / 川 / lomn三
after:  (no Japanese text, marked ignored)
```

**#1246 Equipment menu: side-by-side columns merge into one line.**

```text
before: どうぐぶくろ / 皮の肩 / 皮のぼうし / くそうびぶくろ / 皮のよろい / だいじなもの / やくそう / … / なめした皮で / 作られた / 軽装のよろい
after:  $バウレ ロバどうのつるぎ なめした皮で / どうぐぶくろ 公皮のぼうし 皮の盾 作られた 軽装のよろい / そうびぶくろ 皮のよろい / …
```

**#476 Junk from another window.** The dialogue is right, but two lines of other text came
along.

```text
before: バカな ! / そんな話 / ローラ姫の側近 / はなし / 信じられるか / しん / 川 / 川
after:  神社の注意文をソースと、 / 「2026040412:34:561ニニニニニニニニニニニニニニニニニニニニ / ローラ姫の側近 / バカな! / そんな話信じられるか!
```

**#3933 Game text recovered, window title still read.**

```text
before: 山 / フリット / GameGengo ゲーム言語
after:  フリット / 考えすぎなのかな…。 / 動戦士ガンダムAGE / GGGameGengoゲーム言語
```

## Decisions before the real run

1. **Mark empty sources ignored (`--ignore-empty`): recommended.** About a third of the
   sample (69 of 200) came back with no Japanese text. That projects to about 3,500 of
   10,180. The worker would reject these captures today. Without the flag they stay
   `pending` with empty text and clutter the review queue.
2. **Drop the 392 sources whose images are gone (`--ignore-missing-images`):
   recommended.** They hold 1,849 links that can't be re-checked, and most are junk: コー
   and `t` in 254 of them, 川 in 114, STA in 113. The flag keeps their raw_text, drops their
   auto-promoted links and marks them `ignored`.
3. **Source translations: leave them alone (the default).** The English translations of
   the old EasyOCR text stay attached to the new text.
   - `--requeue-translations` would queue about 6,300 sources again, at one Google request
     per line, while Google is returning 429.
   - The worker also retries `failed` translations with no pause between attempts, so a
     long run of 429s would keep it hitting Google.
   - Re-queue later, after that retry loop is fixed.
4. **Fill term meanings when Google answers again.** The meaning step can run on its own
   at any time: `--limit 0 --skip-rebuild-stats`. It only touches terms that still have no
   meaning, and it stops by itself while the translator fails.
5. **Leave the terms that lose all their sources (about 3,300) for a separate cleanup.**
   Deleting them would also mean deleting their flashcards and Anki notes, so it needs its
   own decision.
6. **Accept the new index.** It is additive, and `db_connect` creates it.

What this rebuild does not fix: captures of the whole desktop (window titles, icon
fragments), merged menu columns, the characters meikiocr drops, and Sudachi's number and
Latin tokens.

## Plan for the real run

The rebuild runs on a copy of the catalog. The copy then replaces the live file, so the
live database is never written while the rebuild is running. The untouched live file
becomes the backup.

Run everything from the repo root, `C:\Users\paulo\Documents\work\Lang`, in PowerShell,
with:

```powershell
$py = ".venv-capture312\Scripts\python.exe"
```

### 0. Before you start

- Merge this branch into `main` and run the commands from the main checkout.
- Plan for 2–3 hours with no capturing, for example overnight, and keep the PC awake.

### 1. Stop everything that writes to the catalog

The desktop capture daemon inserts `capture_jobs` straight into the database and starts
the worker. The web UI writes reviews. Stop all of them:

- Quit the capture daemon from its tray icon.
- Close the web UI and any Anki sync.
- Check that nothing is left running and no job is waiting:

```powershell
Get-Process python*, capture* -ErrorAction SilentlyContinue
@'
import sqlite3
c = sqlite3.connect('file:JP/data/lang_jp.sqlite3?mode=ro', uri=True)
print(c.execute("SELECT status, COUNT(*) FROM capture_jobs WHERE status IN ('pending', 'processing') GROUP BY 1").fetchall())
'@ | & $py -
```

The first command should print nothing and the second `[]`. If jobs are pending, start
the daemon, let it drain them, and stop it again.

### 2. Back up the live database

SQLite's backup API gives a consistent copy even with a WAL file:

```powershell
New-Item -ItemType Directory -Force JP\data\backups | Out-Null
$stamp = Get-Date -Format yyyyMMdd-HHmm
$backup = "JP\data\backups\lang_jp.pre-rebuild-$stamp.sqlite3"
@'
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
source.backup(target)
target.close()
source.close()
check = sqlite3.connect(sys.argv[2])
print('integrity', check.execute('PRAGMA integrity_check').fetchone()[0])
for table in ('sources', 'terms', 'source_terms'):
    print(table, check.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
'@ | & $py - JP\data\lang_jp.sqlite3 $backup
Get-FileHash $backup
```

It should print `integrity ok` and the current counts (10,392 sources, 8,310 terms and
95,675 source_terms on 2026-10-21, more if you captured since). Copy `$backup` to another
drive or to cloud storage before going on.

### 3. Rebuild a working copy

```powershell
New-Item -ItemType Directory -Force JP\data\rebuild | Out-Null
$copy = "JP\data\rebuild\lang_jp.rebuild.sqlite3"
Copy-Item $backup $copy
& $py JP\rebuild_auto_sources.py --db $copy --jp-root JP --ignore-empty --ignore-missing-images
```

- **Expected time:** about 1.5–2 h for 10,180 sources, then the meaning step and about 8
  min of `rebuild-stats`.
- **Output:** progress goes to `$copy.rebuild-progress.jsonl` and the OCR log to
  `$copy.rebuild.log`. The last lines print the totals.
- **If it stops** (Ctrl+C, crash, reboot), run the same command again. It skips finished
  sources and redoes at most the one that was in flight.
- **Batches:** to run in parts, add `--limit 2000 --skip-meanings --skip-rebuild-stats` to
  each batch. Finish with `--limit 0`.
- **Meanings:** if it prints `Stopped filling meanings`, Google is still rate-limiting.
  Carry on. Fill them later with
  `& $py JP\rebuild_auto_sources.py --db JP\data\lang_jp.sqlite3 --allow-live-db --limit 0 --skip-rebuild-stats`
  once the daemon is stopped again.

### 4. Check the copy against the backup

```powershell
@'
import sqlite3, sys
before = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
after = sqlite3.connect(f'file:{sys.argv[2]}?mode=ro', uri=True)
outside = """
    SELECT COUNT(*), COALESCE(SUM(st.count_in_source), 0)
    FROM source_terms AS st JOIN sources AS s ON s.id = st.source_id
    WHERE s.ingest_mode <> 'auto_capture' OR s.review_status IN ('reviewed')
"""
print('integrity', after.execute('PRAGMA integrity_check').fetchone()[0])
print('sources', before.execute('SELECT COUNT(*) FROM sources').fetchone()[0], after.execute('SELECT COUNT(*) FROM sources').fetchone()[0])
print('manual and reviewed links', before.execute(outside).fetchone(), after.execute(outside).fetchone())
print('review status', after.execute('SELECT ingest_mode, review_status, COUNT(*) FROM sources GROUP BY 1, 2').fetchall())
top = after.execute('SELECT t.lemma, t.reading, ts.priority_score FROM terms t JOIN term_stats ts ON ts.term_id = t.id ORDER BY ts.priority_score DESC LIMIT 30')
for lemma, reading, score in top:
    print(f'{score:>9.1f}  {lemma} ({reading})')
'@ | & $py - $backup $copy
```

Before going on, check that:

- `integrity` is `ok`.
- Both source counts match.
- The two `manual and reviewed links` pairs are identical. In the sample run they were
  `(2549, 3561)` both times.
- `review status` shows roughly 3,900 more `ignored` sources.
- The top 30 has no 川/日/一/山/口.

Then spot-check a few sources in the UI pointed at the copy:
`& $py JP\lang_jp.py --db $copy browse`. Use the UI only to look, because reviewing there
writes to the copy. If anything looks wrong, stop here; the live file is untouched.

### 5. Swap the copy in

The daemon is still stopped. Checkpoint the copy into one file, then swap it in:

```powershell
@'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
c.close()
'@ | & $py - $copy
Get-ChildItem JP\data\lang_jp.sqlite3*
Rename-Item JP\data\lang_jp.sqlite3 "lang_jp.replaced-$stamp.sqlite3"
Remove-Item JP\data\lang_jp.sqlite3-wal, JP\data\lang_jp.sqlite3-shm -ErrorAction SilentlyContinue
Move-Item $copy JP\data\lang_jp.sqlite3
```

The checkpoint should print `(0, 0, 0)`.

Before the `Remove-Item` line, `Get-ChildItem` must show `lang_jp.sqlite3-wal` at 0 bytes.
If it doesn't, something wrote to the live file after the backup. Stop and find out what
before you delete anything.

### 6. Restart and check

- Start the capture daemon again.
- Open the web UI and look at the term list and a few rebuilt sources.

### Rolling back

Stop the daemon, then:

```powershell
Rename-Item JP\data\lang_jp.sqlite3 "lang_jp.rebuilt-$stamp.sqlite3"
Remove-Item JP\data\lang_jp.sqlite3-wal, JP\data\lang_jp.sqlite3-shm -ErrorAction SilentlyContinue
Copy-Item $backup JP\data\lang_jp.sqlite3
```

Anything captured after the swap is in the rebuilt file only.
