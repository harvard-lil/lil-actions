"""Say what a deploy left behind, from the outcomes of the steps that decide it.

A deploy that opened a maintenance window ends in one of three states: the
page was lifted, the page is still up because the deployer asked for a hold,
or the page is still up because something went wrong. The last is the one a
person has to act on, and it is the one a run reports least reliably when the
report is a hand-written condition on each consumer's release step. This
reads the same four facts every consumer has -- whether a window opened,
whether a hold was requested, and the outcomes of the migrate and release
steps -- and fails the run only when the page is up and nobody asked for it.
"""
import os
import uuid


def page_state(window, hold, migrate_outcome, release_outcome, site_url):
    """Return (line, left_behind, deliberate) for the maintenance page."""
    if not window:
        return 'Maintenance page: never shown (no window needed)', False, False
    if migrate_outcome not in ('success', 'skipped'):
        return (f'Maintenance page: STILL UP at {site_url} (migrations {migrate_outcome})', True, False)
    if hold:
        return f'Maintenance page: STILL UP at {site_url}, held deliberately', False, True
    if release_outcome == 'success':
        return 'Maintenance page: shown for the deploy, now lifted', False, False
    return f'Maintenance page: STILL UP at {site_url} (release {release_outcome})', True, False


def migration_state(migrate_outcome):
    if migrate_outcome == 'success':
        return 'Migrations: applied (or none pending)'
    if migrate_outcome == 'skipped':
        return 'Migrations: never ran'
    return f'Migrations: {migrate_outcome}; check the database before doing anything else'


def write_outputs(summary, left_behind):
    delimiter = f'ghadelim_{uuid.uuid4().hex}'
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'summary<<{delimiter}\n{summary}\n{delimiter}\n')
        output.write(f'left-behind={str(left_behind).lower()}\n')


def main():
    window = os.environ.get('WINDOW', '').strip() == 'true'
    hold = os.environ.get('HOLD', '').strip() == 'true'
    migrate_outcome = os.environ.get('MIGRATE_OUTCOME', '').strip() or 'skipped'
    release_outcome = os.environ.get('RELEASE_OUTCOME', '').strip() or 'skipped'
    site_url = os.environ.get('SITE_URL', '').strip() or 'the site'
    sources = os.environ.get('SOURCES', '').strip()
    extra_lines = [line.strip() for line in os.environ.get('EXTRA_LINES', '').splitlines() if line.strip()]
    extra_left_behind = os.environ.get('EXTRA_LEFT_BEHIND', '').strip() == 'true'

    page, left_behind, deliberate = page_state(window, hold, migrate_outcome, release_outcome, site_url)
    lines = [page, migration_state(migrate_outcome)] + extra_lines
    left_behind = left_behind or extra_left_behind

    summary = ['*State:*'] + lines
    if sources:
        summary.append(f'*Deploy flags:* {sources}')
    summary = '\n'.join(summary)
    write_outputs(summary, left_behind)
    print('\n'.join(lines))

    if left_behind:
        print('::error::This deploy left something switched off that needs a person:')
        for line in lines:
            if 'STILL' in line:
                print(f'::error::{line}')
        if migrate_outcome not in ('success', 'skipped'):
            print('::error::The database may be partially migrated. Check its state before lifting the page.')
        print('::error::Re-running this workflow will not lift it; it will stop at the same step.')
        raise SystemExit(1)
    if deliberate:
        print('::notice::Deploy verified. MAINTENANCE MODE IS STILL ON and was left on deliberately: '
              f'{site_url} is serving the maintenance page because a hold was requested'
              + (f' ({sources})' if sources else '') + '.')
        print('::notice::To release it, dispatch this workflow again with force-maintenance and without '
              'hold-maintenance (unset the hold variable first if the hold came from it), or detach the '
              'maintenance route in Cloudflare.')
    else:
        print('Nothing left switched off.')


if __name__ == '__main__':
    main()
