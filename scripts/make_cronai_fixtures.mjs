// Rebuilds tests/fixtures/cronai_runs.json from the HOSTED cronai engine, the same
// code the instruction page's <cron-ai> widget runs:
//
//   node scripts/make_cronai_fixtures.mjs
//
// For each phrase (and a few raw cron lines) it records what the widget SAVES and
// the runs the widget PROMISES. tests/test_cronai_schedule.py then checks that our
// scheduler fires at exactly those moments. Nothing of cronai is vendored: the
// engine is downloaded to a temp file for the run and thrown away.
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const ENGINE = 'https://abozaralizadeh.github.io/cronai/widget/cronai-engine.esm.js';
const OUT = new URL('../tests/fixtures/cronai_runs.json', import.meta.url);

const response = await fetch(ENGINE);
if (!response.ok) throw new Error(`${ENGINE}: HTTP ${response.status}`);
const file = join(mkdtempSync(join(tmpdir(), 'cronai-')), 'engine.mjs');
writeFileSync(file, await response.text());
const { parseSchedule, scheduleNextRuns } = await import(pathToFileURL(file).href);

const FROMS = [
  '2026-10-04T10:00:00Z',   // an ordinary week
  '2026-10-23T12:00:00Z',   // Europe falls back on 25 Oct 2026 (02:00-03:00 happens twice)
  '2027-03-26T12:00:00Z',   // Europe springs forward on 28 Mar 2027 (02:00-03:00 never happens)
  '2026-10-30T12:00:00Z',   // the US falls back on 1 Nov 2026
];
const ZONES = ['Europe/Rome', 'UTC', 'America/New_York'];
const RUNS = 6;

const PHRASES = [
  'every day at 9am', 'every weekday at 9:30', 'at 16:00 on tuesday, thursday and sunday',
  'every monday wednesday and saturday at 4pm', 'twice a day', 'every 15 minutes',
  'every 90 minutes', 'every 3 hours', 'every hour during business hours',
  'every 10 min from 9 to 5', 'last friday of every month at 6pm',
  'second tuesday of the month at 10am', 'last day of the month at 23:00',
  'every two weeks on saturday', 'every other friday at 5pm',
  'every 3 weeks on monday and thursday at 8am',
  'evrey wensday and fridy at half past 4 in the afternoon except in august',
  '2:30am every night', 'every sunday at midnight', 'on the 1st and 15th at noon',
  'quarter to 10 on weekends', '1st of august at midnight', 'every 4 hours on weekdays',
  'every 2 hours from 8am to 8pm', 'at 6am and 6pm', '17h30 daily', 'every 30 minutes on mondays',
];

// Raw cron lines, which the widget also accepts as typed: the field grammar itself.
const RAW = [
  '0 9 1 * 1',          // day of month OR weekday (classic cron), not AND
  '0 18 * * 5L',        // last Friday of the month
  '0 9 * * 1#2',        // second Monday
  '0 9 * * 6#1,6#3',    // first and third Saturday
  '0 0 L * *',          // last day of the month
  '0 16 * * 4',         // Thursday: 4 is Thursday in cron, Friday in APScheduler
  '30 2 * * *',         // inside Europe's DST gap and overlap
  '0 9 * * MON-FRI', '0 9 * * FRI-MON', '0 9 1-7 * MON', '*/20 9-10 * JAN-MAR,OCT *',
  '0 12 * * 0', '0 12 * * 7', '15 */6 * * *', '0 0 29 2 *',
];

const iso = (d) => d.toISOString().replace('.000Z', 'Z');
const runsFor = (schedule) =>
  Object.fromEntries(FROMS.map((from) => [from, scheduleNextRuns(schedule, RUNS, new Date(from)).map((x) => iso(x.date))]));

const cases = [];
for (const text of PHRASES) {
  for (const timezone of ZONES) {
    const r = parseSchedule(text, { timezone });
    if (!r.ok) throw new Error(`cronai did not understand "${text}" (${timezone}): ${r.error}`);
    cases.push({ schedule: r.schedule, runs: runsFor(r.schedule) });
  }
}
for (const cron of RAW) {
  for (const timezone of ['Europe/Rome', 'UTC']) {
    const schedule = { v: 1, text: cron, crons: [cron], timezone, description: '' };
    cases.push({ raw: true, schedule, runs: runsFor(schedule) });
  }
}

const head = JSON.stringify({ source: ENGINE, generated: new Date().toISOString().slice(0, 10), cases: [] }, null, 1);
writeFileSync(OUT, head.replace('"cases": []', `"cases": [\n${cases.map((c) => JSON.stringify(c)).join(',\n')}\n]`) + '\n');
console.log(`${cases.length} schedules × ${FROMS.length} start points written to tests/fixtures/cronai_runs.json`);
