#!/usr/bin/env python
import logging
import yara  # type: ignore
import config
import json
import sys
from lib.ursadb import UrsaDb
from util import setup_logging
from typing import Any, List
from lib.yaraparse import parse_yara, combine_rules
from db import AgentTask, JobId, Database, MatchInfo, TaskType
from cachetools import cached, LRUCache
from metadata import MetadataPlugin, Metadata
from plugins import METADATA_PLUGINS


@cached(cache=LRUCache(maxsize=32), key=lambda db, job: job.key)
def compile_yara(db: Database, job: JobId) -> Any:
    """Gets a compiled yara rule belinging to the provided job. Uses cache
    to speed up compilation.

    :param job: ID of the job to compile yara for.
    :type job: JobId
    :raises SyntaxError: When yara rule has invalid syntax.
    :return: Compiled yara rule.
    :rtype: Any
    """
    yara_rule = db.get_yara_by_job(job)

    logging.info("Compiling Yara")
    try:
        rule = yara.compile(source=yara_rule)
    except SyntaxError as e:
        logging.exception("Yara parse error")
        raise e

    return rule


class Agent:
    def __init__(self, group_id: str, ursa_url: str, db: Database) -> None:
        """Creates a new agent instance. Every agents belongs to some group
        (identified by `group_id`). There may be multiple agents in a
        single group, but they're all exchangeable (they read and write to the
        same queues, and they use the same ursadb instance).

        :param group_id: Identifier of the agent group this agent belongs to.
        :type group_id: str
        :param ursa_url: URL to connected ursadb instance. Ideally this should
            be public, because this will allow mquery to collect measurements.
        :type ursa_url: str
        :param db: Reference to main database/task queue.
        :type db: Database
        """
        self.group_id = group_id
        self.ursa_url = ursa_url
        self.db = db
        self.ursa = UrsaDb(self.ursa_url)
        self.active_plugins: List[MetadataPlugin] = []

    def __search_task(self, job_id: JobId) -> None:
        """Do ursadb query for yara belonging to the provided job.
        If successful, create a new yara tasks to do further processing
        of the results.
        """
        logging.info("Parsing...")

        job = self.db.get_job(job_id)
        if job.status == "cancelled":
            logging.info("Job was cancelled, returning...")
            return

        if job.status == "new":
            # First search request - find datasets to query
            logging.info("New job, generate subtasks...")
            result = self.ursa.topology()
            if "error" in result:
                raise RuntimeError(result["error"])
            self.db.init_job_datasets(
                self.group_id,
                job_id,
                list(result["result"]["datasets"].keys()),
            )

        logging.info("Get next dataset to query...")
        dataset = self.db.get_next_search_dataset(self.group_id, job_id)
        if dataset is None:
            logging.info("Nothing to query, returning...")
            return

        rules = parse_yara(job.raw_yara)
        parsed = combine_rules(rules)

        logging.info("Querying backend...")
        result = self.ursa.query(parsed.query, job.taint, dataset)
        if "error" in result:
            raise RuntimeError(result["error"])

        file_count = result["file_count"]
        iterator = result["iterator"]
        logging.info(f"Iterator {iterator} contains {file_count} files")

        self.db.update_job_files(job_id, file_count)
        self.db.agent_start_job(self.group_id, job_id, iterator)
        self.db.agent_continue_search(self.group_id, job_id)

    def __load_plugins(self) -> None:
        self.plugin_config_version = self.db.get_plugin_config_version()
        active_plugins = []
        for plugin_class in METADATA_PLUGINS:
            plugin_name = plugin_class.get_name()
            plugin_config = self.db.get_plugin_configuration(plugin_name)
            try:
                active_plugins.append(plugin_class(self.db, plugin_config))
                logging.info("Loaded %s plugin", plugin_name)
            except Exception:
                logging.exception("Failed to load %s plugin", plugin_name)
        self.active_plugins = active_plugins

    def __initialize_agent(self) -> None:
        self.__load_plugins()
        plugins_spec = {
            plugin_class.get_name(): plugin_class.config_fields
            for plugin_class in METADATA_PLUGINS
        }
        self.db.register_active_agent(
            self.group_id,
            self.ursa_url,
            plugins_spec,
            [
                active_plugin.get_name()
                for active_plugin in self.active_plugins
            ],
        )

    def __update_metadata(
        self, job: JobId, file_path: str, matches: List[str]
    ) -> None:
        """ After finding a match, push it into a database and
        update the related metadata """
        metadata: Metadata = {}
        for plugin in self.active_plugins:
            try:
                extracted_meta = plugin.run(file_path, metadata)
                metadata.update(extracted_meta)
            except Exception:
                logging.exception(
                    "Failed to launch plugin %s for %s",
                    plugin.get_name(),
                    file_path,
                )
        match = MatchInfo(file_path, metadata, matches)
        self.db.add_match(job, match)

    def __execute_yara(self, job: JobId, files: List[str]) -> None:
        rule = compile_yara(self.db, job)
        num_matches = 0
        self.db.job_start_work(job, len(files))
        for sample in files:
            try:
                matches = rule.match(sample)
                if matches:
                    num_matches += 1
                    self.__update_metadata(
                        job, sample, [r.rule for r in matches]
                    )
            except yara.Error:
                logging.exception(f"Yara failed to check file {sample}")
            except FileNotFoundError:
                logging.exception(
                    f"Failed to open file for yara check: {sample}"
                )

        self.db.job_update_work(job, len(files), num_matches)

    def __yara_task(self, job: JobId, iterator: str) -> None:
        """Get a next batch of worm from the db. If there are still files
        left in the iterator, push the task back to the same queue (so
        that other agents will be able to work on it in parallel). Later,
        process the obtained files.
        """
        final_statuses = ["cancelled", "failed", "done"]
        j = self.db.get_job(job)
        if j.status in final_statuses:
            return

        MIN_BATCH_SIZE = 10
        MAX_BATCH_SIZE = 500

        taken_files = j.files_processed + j.files_in_progress

        # Never do more than MAX_BATCH_SIZE files at once.
        batch_size = MAX_BATCH_SIZE

        # Take small batches of work at first, so the db appears to run faster.
        batch_size = min(batch_size, taken_files)

        # Don't take more than 1/4 of files left at once (to speed up finishes).
        batch_size = min(batch_size, (j.total_files - taken_files) // 4)

        # Finally, always process at least MIN_BATCH_SIZE files.
        batch_size = max(batch_size, MIN_BATCH_SIZE)

        pop_result = self.ursa.pop(iterator, batch_size)
        if not pop_result.iterator_empty:
            # The job still have some files, put it back on the queue.
            self.db.agent_start_job(self.group_id, job, iterator)
        if pop_result.files:
            # If there are any files popped iterator, work on them
            self.__execute_yara(job, pop_result.files)

        j = self.db.get_job(job)
        if j.status == "processing" and j.files_processed == j.total_files:
            # The job is over, work of this agent as done.
            self.db.agent_finish_job(job)

    def __process_task(self, task: AgentTask) -> None:
        """Dispatches and executes the next incoming task.

        The high level workflow look like this: for every new `search` job,
        mquery creates a new `search` task for every agent group.
        One of the agents will pick it up and execute, and create `yara`
        tasks. `yara` tasks will be executed by workers for every file in
        iterator, until it's exhausted.

        :param task: Task to be executed.
        :type task: AgentTask
        :raises RuntimeError: Task with unsupported type given.
        """
        if task.type == TaskType.RELOAD:
            if (
                self.plugin_config_version
                == self.db.get_plugin_config_version()
            ):
                # This should never happen and suggests that there is bug somewhere
                # and version was not updated properly.
                logging.error(
                    "Critical error: Requested to reload configuration, but "
                    "configuration present in database is still the same (%s).",
                    self.plugin_config_version,
                )
                return
            logging.info("Configuration changed - reloading plugins.")
            # Request next agent to reload the configuration
            self.db.reload_configuration(self.plugin_config_version)
            # Reload configuration. Version will be updated during reinitialization,
            # so we don't receive our own request.
            self.__initialize_agent()
        elif task.type == TaskType.SEARCH:
            job = JobId(task.data)
            logging.info(f"search: {job.hash}")

            try:
                self.__search_task(job)
            except Exception as e:
                logging.exception("Failed to execute task.")
                self.db.agent_finish_job(job)
                self.db.fail_job(job, str(e))
        elif task.type == TaskType.YARA:
            data = json.loads(task.data)
            job = JobId(data["job"])
            iterator = data["iterator"]
            logging.info("yara: iterator %s", iterator)

            try:
                self.__yara_task(job, iterator)
            except Exception as e:
                logging.exception("Failed to execute task.")
                self.db.agent_finish_job(job)
                self.db.fail_job(job, str(e))
        else:
            raise RuntimeError("Unsupported queue")

    def main_loop(self) -> None:
        """Starts a main loop of the agent - this is the only intended public
        method of this class. This will register the agent in the db, then pop
        tasks from redis as they come, and execute them.
        """
        self.__initialize_agent()

        while True:
            task = self.db.agent_get_task(
                self.group_id, self.plugin_config_version
            )
            self.__process_task(task)


def main() -> None:
    """Spawns a new agent process. Use argv if you want to use a different
    group_id (it's `default` by default)
    """
    setup_logging()
    if len(sys.argv) > 1:
        agent_group_id = sys.argv[1]
    else:
        agent_group_id = "default"

    logging.info("Agent [%s] running...", agent_group_id)

    db = Database(config.REDIS_HOST, config.REDIS_PORT)
    agent = Agent(agent_group_id, config.BACKEND, db)

    agent.main_loop()


if __name__ == "__main__":
    main()
