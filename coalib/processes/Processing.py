import multiprocessing
import queue

from coalib.collecting.Collectors import collect_files
from coalib.collecting import Dependencies
from coalib.output.printers import LOG_LEVEL
from coalib.processes.BearRunner import BearRunner
from coalib.processes.CONTROL_ELEMENT import CONTROL_ELEMENT
from coalib.results.HiddenResult import HiddenResult
from coalib.settings.Setting import path_list
from coalib.misc.i18n import _
from coalib.processes.LogPrinterThread import LogPrinterThread


def get_cpu_count():
    try:
        return multiprocessing.cpu_count()
    # cpu_count is not implemented for some CPU architectures/OSes
    except NotImplementedError:  # pragma: no cover
        return 2


def fill_queue(queue_fill, any_list):
    """
    Takes element from a list and populates a queue with those elements.

    :param queue_fill: The queue to be filled.
    :param any_list:   List containing the elements.
    """
    for elem in any_list:
        queue_fill.put(elem)


def get_running_processes(processes):
    return sum((1 if process.is_alive() else 0) for process in processes)


def print_result(result_dict,
                 file_dict,
                 index,
                 retval,
                 print_results):
    """
    Takes the results produced by each bear and gives them to the interactor to
    present to the user.

    :param result_dict:   A dictionary containing results.
    :param file_dict:     A dictionary containing the name of files and its
                          contents.
    :param index:         It is the index indicating which result to print.
    :param retval:        It is True if no results were yielded ever before.
                          If it is False this function will return False no
                          matter what happens. Else it depends on if this
                          invocation yields results.
    :param print_results: Prints all given results appropriate to the
                          output medium.
    :return:              Returns False if any results were yielded. Else True.
    """
    results = list(filter(lambda result: not isinstance(result, HiddenResult),
                          result_dict[index]))
    print_results(results, file_dict)

    return retval or len(results) > 0


def get_file_dict(filename_list, log_printer):
    """
    Reads all files into a dictionary.

    :param filename_list: List of names of paths to files to get contents of.
    :param log_printer:   The logger which logs errors.
    :return:              Reads the content of each file into a dictionary
                          with filenames as keys.
    """
    file_dict = {}
    for filename in filename_list:
        try:
            with open(filename, "r", encoding="utf-8") as f:
                file_dict[filename] = f.readlines()
        except UnicodeDecodeError:
            log_printer.warn(_("Failed to read file '{}'. It seems to contain "
                               "non-unicode characters. Leaving it "
                               "out.".format(filename)))
        except Exception as exception:  # pragma: no cover
            log_printer.log_exception(_("Failed to read file '{}' because of "
                                        "an unknown error. Leaving it "
                                        "out.").format(filename),
                                      exception,
                                      log_level=LOG_LEVEL.WARNING)

    return file_dict


def instantiate_bears(section,
                      local_bear_list,
                      global_bear_list,
                      file_dict,
                      message_queue):
    """
    Instantiates each bear with the arguments it needs.

    :param section:          The section the bears belong to.
    :param local_bear_list:  List of local bears to instantiate.
    :param global_bear_list: List of global bears to instantiate.
    :param file_dict:        Dictionary containing filenames and their
                             contents.
    :param message_queue:    Queue responsible to maintain the messages
                             delivered by the bears.
    """
    for i in range(len(local_bear_list)):
        local_bear_list[i] = local_bear_list[i](section,
                                                message_queue,
                                                TIMEOUT=0.1)
    for i in range(len(global_bear_list)):
        global_bear_list[i] = global_bear_list[i](file_dict,
                                                  section,
                                                  message_queue,
                                                  TIMEOUT=0.1)


def instantiate_processes(section,
                          local_bear_list,
                          global_bear_list,
                          job_count,
                          log_printer):
    """
    Instantiate the number of BearRunner objects or processes that will run
    bears which will be responsible for running bears in a multiprocessing
    environment.

    :param section:          The section the bears belong to.
    :param local_bear_list:  List of local bears belonging to the section.
    :param global_bear_list: List of global bears belonging to the section.
    :param job_count:        Max number of processes to create.
    :param log_printer:      The log printer to warn to.
    :return:                 A tuple containing a list of BearRunner objects,
                             and the arguments passed to each object which are
                             the same for each object.
    """
    filename_list = collect_files(path_list(section.get('files', "")),
                                  log_printer)
    file_dict = get_file_dict(filename_list, log_printer)

    manager = multiprocessing.Manager()
    global_bear_queue = multiprocessing.Queue()
    filename_queue = multiprocessing.Queue()
    local_result_dict = manager.dict()
    global_result_dict = manager.dict()
    message_queue = multiprocessing.Queue()
    control_queue = multiprocessing.Queue()

    bear_runner_args = {"file_name_queue": filename_queue,
                        "local_bear_list": local_bear_list,
                        "global_bear_list": global_bear_list,
                        "global_bear_queue": global_bear_queue,
                        "file_dict": file_dict,
                        "local_result_dict": local_result_dict,
                        "global_result_dict": global_result_dict,
                        "message_queue": message_queue,
                        "control_queue": control_queue,
                        "TIMEOUT": 0.1}

    instantiate_bears(section,
                      local_bear_list,
                      global_bear_list,
                      file_dict,
                      message_queue)
    fill_queue(filename_queue, file_dict.keys())
    fill_queue(global_bear_queue, range(len(global_bear_list)))

    return ([BearRunner(**bear_runner_args) for i in range(job_count)],
            bear_runner_args)


def process_queues(processes,
                   control_queue,
                   local_result_dict,
                   global_result_dict,
                   file_dict,
                   print_results,
                   finalize):
    """
    Iterate the control queue and send the results recieved to the interactor
    so that they can be presented to the user.

    :param processes:          List of processes which can be used as
                               BearRunners.
    :param control_queue:      Containing control elements that indicate
                               whether there is a result available and which
                               bear it belongs to.
    :param local_result_dict:  Dictionary containing results respective to
                               local bears. It is modified by the processes
                               i.e. results are added to it by multiple
                               processes.
    :param global_result_dict: Dictionary containing results respective to
                               global bears. It is modified by the processes
                               i.e. results are added to it by multiple
                               processes.
    :param file_dict:          Dictionary containing file contents with
                               filename as keys.
    :param print_results:      Prints all given results appropriate to the
                               output medium.
    :param finalize:           This method is called after all results have
                               been sent for printing.
    :return:                   Return True if all bears execute succesfully and
                               Results were delivered to the user. Else False.
    """
    running_processes = get_running_processes(processes)
    retval = False
    # Number of processes working on local bears
    local_processes = len(processes)
    global_result_buffer = []

    # One process is the logger thread
    while local_processes > 1 and running_processes > 1:
        try:
            control_elem, index = control_queue.get(timeout=0.1)

            if control_elem == CONTROL_ELEMENT.LOCAL_FINISHED:
                local_processes -= 1
            elif control_elem == CONTROL_ELEMENT.LOCAL:
                assert local_processes != 0
                retval = print_result(local_result_dict,
                                      file_dict,
                                      index,
                                      retval,
                                      print_results)
            elif control_elem == CONTROL_ELEMENT.GLOBAL:
                global_result_buffer.append(index)
        except queue.Empty:
            running_processes = get_running_processes(processes)

    # Flush global result buffer
    for elem in global_result_buffer:
        retval = print_result(global_result_dict,
                              file_dict,
                              elem,
                              retval,
                              print_results)

    running_processes = get_running_processes(processes)
    # One process is the logger thread
    while running_processes > 1:
        try:
            control_elem, index = control_queue.get(timeout=0.1)

            if control_elem == CONTROL_ELEMENT.GLOBAL:
                retval = print_result(global_result_dict,
                                      file_dict,
                                      index,
                                      retval,
                                      print_results)
            else:
                assert control_elem == CONTROL_ELEMENT.GLOBAL_FINISHED
                running_processes = get_running_processes(processes)

        except queue.Empty:
            running_processes = get_running_processes(processes)

    finalize(file_dict)
    return retval


def execute_section(section,
                    global_bear_list,
                    local_bear_list,
                    print_results,
                    finalize,
                    log_printer):
    """
    Executes the section with the given bears.

    The execute_section method does the following things:
    1. Prepare a BearRunner
      * Load files
      * Create queues
    2. Spawn up one or more BearRunner's
    3. Output results from the BearRunner's
    4. Join all processes

    :param section:          The section to execute.
    :param global_bear_list: List of global bears belonging to the section.
    :param local_bear_list:  List of local bears belonging to the section.
    :param print_results:    Prints all given results appropriate to the
                             output medium.
    :param finalize:         This method is called after all results have been
                             sent for printing.
    :param log_printer:      The log_printer to warn to.
    :return:                 Tuple containing a bool (True if results were
                             yielded, False otherwise), a Manager.dict
                             containing all local results(filenames are key)
                             and a Manager.dict containing all global bear
                             results (bear names are key).
    """
    local_bear_list = Dependencies.resolve(local_bear_list)
    global_bear_list = Dependencies.resolve(global_bear_list)

    running_processes = get_cpu_count()
    processes, arg_dict = instantiate_processes(section,
                                                local_bear_list,
                                                global_bear_list,
                                                running_processes,
                                                log_printer)

    logger_thread = LogPrinterThread(arg_dict["message_queue"],
                                     log_printer)
    # Start and join the logger thread along with the BearRunner's
    processes.append(logger_thread)

    for runner in processes:
        runner.start()

    try:
        return (process_queues(processes,
                               arg_dict["control_queue"],
                               arg_dict["local_result_dict"],
                               arg_dict["global_result_dict"],
                               arg_dict["file_dict"],
                               print_results,
                               finalize),
                arg_dict["local_result_dict"],
                arg_dict["global_result_dict"])
    finally:
        logger_thread.running = False

        for runner in processes:
            runner.join()