"""Saved links and the page archive: save, capture (render + freeze + extract), storage, jobs.

Entry points other packages use (import lazily): ``save.save_link``, ``save.archive_after_star``,
``save.find_url``, ``storage.get_storage``. The capture job runs on its own queue and worker.
"""
