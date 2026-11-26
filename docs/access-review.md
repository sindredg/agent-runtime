# Access review generator

`packages/access_review` builds a synthetic organisation from a seed and
renders it as one job: seed files, `system` and a prompt. From the job's
text it reads two answer keys, and it prints a hash commitment to the seed,
the keys and the job. Standard library only.

![A seed and a shape give an organisation, rendered as the corpus files with their index and as the prompt. With the fixed system text they make the job file. The files give one answer key, the files and the notices the other. The commitment hashes the seed, both keys and a digest of the job.](diagrams/access-review.svg)

## The organisation

- One team per corpus file. A team has 32 people, three of them leavers who
  hold no grant.
- 60 systems; every third one needs approval. A file lists 16: 5 that need
  approval and 11 that do not.
- A grant gives one person one system with a role: `reader`, `editor`,
  `operator` or `admin`. A person holds at most one grant per system.
- Names are invented words; ids are random. Every draw comes from SHA-256
  over the seed, the generator version, the shape and a label, so one seed
  gives one job.

## What a file holds

A file is one team's records, exactly 16,384 bytes of ASCII:

```
access records, file 0147 of 0293
team: Rusen Tozel (TM7K2M9QX4)
[systems]
Krason Svelom (SY3K9P2A): approval required
[roster 2026-03-31]
Bakod Mevuk (PN7K2M9QX4)
[roster 2026-06-30]
Bakod Mevuk (PN7K2M9QX4)
[grants]
GR5T0V8K2M9Q: Bakod Mevuk (PN7K2M9QX4), Krason Svelom (SY3K9P2A), editor, granted 2025-04-17
[leaver notices]
[approvals]
Approval: the owner of Krason Svelom (SY3K9P2A) approved access for Bakod Mevuk (PN7K2M9QX4) on 2026-05-02.
[end] ======
```

- Clean grants fill the file up to its size. The `[end]` line pads the rest.
- Lines of each section are sorted by id, so a line's place says nothing.
- `index.txt` lists the corpus paths, one per line, and nothing else.

## The rules

The prompt states them:

1. A grant is a violation when its holder has left: a leaver notice names
   them in any file, or they are on a roster of their team and missing from
   a later one.
2. A grant on a system marked `approval required` is a violation unless an
   approval in its file names its holder and that system.
3. Nothing else is a violation.

The rules are part of the task text, so a summary has to carry them.
`system` holds three fixed sentences: the index path, the title format of
an issue, and "Open each issue when you find it."

## Late records

The prompt also carries 16 records that "arrived after the files were
written": 12 leaver notices and 4 approvals. They count like records in the
files. Each is written by the same function as the records in the files.

## The two keys

| Key | Read from | Grants |
|---|---|---|
| `key_files` | The files alone | 24 |
| `key_full` | The files and the prompt's records | 32 |

The 12 late notices add grants (`made`); the 4 late approvals clear grants
(`cleared`). Both keys are read from the job's text, never from the model
it was built from.

| Planted kind | Count | What decides it |
|---|---|---|
| `late_leaver` | 12 | A notice in the prompt only |
| `late_approval` | 4 | An approval in the prompt only |
| `local_notice`, `local_roster`, `local_approval` | 4 each | A notice, a roster gap or a missing approval in the grant's file |
| `carried` | 8 | A notice in the file `lead_rows` rows before the grant's file |

Every other grant is clean. Each planted grant has exactly one reason.

## Shapes

| Shape | Files | `lead_rows` | Corpus bytes | Index bytes |
|---|---|---|---|---|
| `full` | 293 | 24 | 4,800,512 | 4,688 |
| `short` | 36 | 6 | 589,824 | 576 |

Both fit the 8 MiB limit on a spec's files
([ADR 0032](adr/0032-seed-files-up-to-8-mib.md)).

## The command

```
uv run python -m access_review commitment --shape full --seed-file PATH [--write-preimage PATH]
```

The seed file is JSON with a string `seed` of at least 32 hex digits.
Stdout is four lines: `generator`, `shape`, `job` and `commitment`.

- `job` is the SHA-256 of the job's files, prompt and `system`.
- `commitment` is the SHA-256 of a JSON preimage: the generator version,
  the job digest, both keys with `made` and `cleared`, the reference code,
  the seed and the shape.
- `--write-preimage` writes those bytes. It refuses a path inside a checkout.

Every check runs before anything is printed. A failed check exits 1 with
lines of counts only; a refused file or seed exits 2 with a fixed sentence.
Neither quotes the seed.

The unit tests use `tests/unit/access_review_seed.json`, which pins the
job digest and commitment of each shape. This seed is public, so
everything generated from it is known, the answer key included.

## Limits

- The size is set in bytes, not tokens.
- No model has run on it.
- The organisation is generated, and its closeness to a real one is not
  shown.
- A seed can fail a check. The command then prints no hash; the tests run
  the checks on 22 other seeds.
