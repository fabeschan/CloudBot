import inspect
import re
import _thread
import queue
from queue import Empty

from core.pluginmanager import CommandPlugin


_thread.stack_size(1024 * 512)  # reduce vm size

_input_name_aliases = {
    "inp": "text",
    "match": "text",
    "paramlist": "paraml"
}


class Input:
    """
    :type bot: core.bot.CloudBot
    :type conn: core.irc.BotConnection
    :type raw: str
    :type prefix: str
    :type command: str
    :type params: str
    :type nick: str
    :type user: str
    :type host: str
    :type mask: str
    :type paraml: list[str]
    :type msg: str
    :type input: Input
    :type text: list[str]
    :type server: str
    :type lastparam: str
    :type chan: str
    """

    def __init__(self, bot, conn, raw, prefix, command, params,
                 nick, user, host, mask, paramlist, msg):
        """
        :type bot: core.bot.CloudBot
        :type conn: core.irc.BotConnection
        :type raw: str
        :type prefix: str
        :type command: str
        :type params: str
        :type nick: str
        :type user: str
        :type host: str
        :type mask: str
        :type paramlist: list[str]
        :type msg: str
        """
        self.bot = bot
        self.conn = conn
        self.raw = raw
        self.prefix = prefix
        self.command = command
        self.params = params
        self.nick = nick
        self.user = user
        self.host = host
        self.mask = mask
        self.paraml = paramlist
        self.msg = msg
        self.input = self
        self.text = self.paraml  # this will be assigned later
        self.server = conn.server
        self.lastparam = paramlist[-1]  # TODO: This is equivalent to msg
        self.chan = paramlist[0].lower()

        if self.chan == conn.nick.lower():  # is a PM
            self.chan = nick

        self.trigger = None  # assigned later?

    def message(self, message, target=None):
        """sends a message to a specific or current channel/user
        :type message: str
        :type target: str
        """
        if target is None:
            target = self.chan
        self.conn.msg(target, message)

    def reply(self, message, target=None):
        """sends a message to the current channel/user with a prefix
        :type message: str
        :type target: str
        """
        if target is None:
            target = self.chan

        if target == self.nick:
            self.conn.msg(target, message)
        else:
            self.conn.msg(target, "({}) {}".format(self.nick, message))

    def action(self, message, target=None):
        """sends an action to the current channel/user or a specific channel/user
        :type message: str
        :type target: str
        """
        if target is None:
            target = self.chan

        self.conn.ctcp(target, "ACTION", message)

    def ctcp(self, message, ctcp_type, target=None):
        """sends an ctcp to the current channel/user or a specific channel/user
        :type message: str
        :type ctcp_type: str
        :type target: str
        """
        if target is None:
            target = self.chan
        self.conn.ctcp(target, ctcp_type, message)

    def notice(self, message, target=None):
        """sends a notice to the current channel/user or a specific channel/user
        :type message: str
        :type target: str
        """
        if target is None:
            target = self.nick

        self.conn.cmd('NOTICE', [target, message])

    def has_permission(self, permission):
        """ returns whether or not the current user has a given permission
        :type permission: str
        :rtype: bool
        """
        return self.conn.permissions.has_perm_mask(self.mask, permission)


def run(bot, plugin, input):
    """
    :type bot: core.bot.CloudBot
    :type plugin: Plugin
    :type input: Input
    """
    bot.logger.debug("Input: {}".format(input.__dict__))

    parameters = []
    specifications = inspect.getargspec(plugin.function)
    required_args = specifications[0]
    if required_args is None:
        required_args = []

    # Does the command need DB access?
    uses_db = "db" in required_args

    if uses_db:
        # create SQLAlchemy session
        bot.logger.debug("Opened DB session for: {}".format(plugin.module.title))
        input.db = input.bot.db_session()

    # all the dynamic arguments
    for required_arg in required_args:
        if required_arg in _input_name_aliases:
            required_arg = _input_name_aliases[required_arg]

        if hasattr(input, required_arg):
            value = getattr(input, required_arg)
            parameters.append(value)
        else:
            bot.logger.error("Plugin {}:{} asked for invalid argument '{}', cancelling execution!"
                             .format(plugin.module.title, plugin.function_name, required_arg))
            return

    try:
        out = plugin.function(*parameters)
    except Exception:
        bot.logger.exception("Error in plugin {}:".format(plugin.module.title))
        bot.logger.info("Parameters used: {}".format(parameters))
        return
    finally:
        if uses_db:
            bot.logger.debug("Closed DB session for: {}".format(plugin.module.title))
            input.db.close()

    if out is not None:
        input.reply(str(out))


def do_sieve(sieve, bot, input, plugin):
    """
    :type sieve: Plugin
    :type bot: core.bot.CloudBot
    :type input: Input
    :type plugin: Plugin
    :rtype: Input
    """
    try:
        return sieve.function(bot, input, plugin)
    except Exception:
        bot.logger.exception("Error running sieve {}:{} on {}:{}:".format(sieve.module.title, sieve.function_name,
                                                                          plugin.module.title, plugin.function_name))
        return None


class Handler:
    """Runs modules in their own threads (ensures order)
    :type bot: core.bot.CloudBot
    :type plugin: Plugin
    :type input_queue: queue.Queue[Input]
    """

    def __init__(self, bot, plugin):
        """
        :type bot: core.bot.CloudBot
        :type plugin: Plugin
        """
        self.bot = bot
        self.plugin = plugin
        self.input_queue = queue.Queue()
        _thread.start_new_thread(self.start, ())

    def start(self):
        while True:
            while self.bot.running:  # This loop will break when successful
                try:
                    plugin_input = self.input_queue.get(block=True, timeout=1)
                    break
                except Empty:
                    pass

            if not self.bot.running or plugin_input == StopIteration:
                break

            run(self.bot, self.plugin, plugin_input)

    def stop(self):
        self.input_queue.put(StopIteration)

    def put(self, value):
        """
        :type value: Input
        """
        self.input_queue.put(value)


def dispatch(bot, input, plugin):
    """
    :type bot: core.bot.CloudBot
    :type input: Input
    :type plugin: core.pluginmanager.Plugin
    """
    for sieve in bot.plugin_manager.sieves:
        input = do_sieve(sieve, bot, input, plugin)
        if input is None:
            return

    if isinstance(plugin, CommandPlugin) and \
            plugin.args.get('autohelp', True) and not input.text and plugin.doc is not None:
        input.notice(input.conn.config["command_prefix"] + plugin.doc)
        return

    if plugin.args.get("singlethread", False):
        if plugin in bot.threads:
            bot.threads[plugin].put(input)
        else:
            bot.threads[plugin] = Handler(bot, plugin)
    else:
        _thread.start_new_thread(run, (bot, plugin, input))


def main(bot, conn, out):
    """
    :type bot: core.bot.CloudBot
    :type conn: core.irc.BotConnection
    :type out: list
    """
    inp = Input(bot, conn, *out)
    command_prefix = conn.config.get('command_prefix', '.')

    # EVENTS
    if inp.command in bot.plugin_manager.events:
        for event_plugin in bot.plugin_manager.events[inp.command]:
            dispatch(bot, Input(bot, conn, *out), event_plugin)
    for event_plugin in bot.plugin_manager.catch_all_events:
        dispatch(bot, Input(bot, conn, *out), event_plugin)

    if inp.command == 'PRIVMSG':
        # COMMANDS
        if inp.chan == inp.nick:  # private message, no command prefix
            prefix = '^(?:[{}]?|'.format(command_prefix)
        else:
            prefix = '^(?:[{}]|'.format(command_prefix)
        command_re = prefix + inp.conn.nick
        command_re += r'[,;:]+\s+)(\w+)(?:$|\s+)(.*)'

        match = re.match(command_re, inp.lastparam)

        if match:
            command = match.group(1).lower()
            if command in bot.plugin_manager.commands:
                plugin = bot.plugin_manager.commands[command]
                input = Input(bot, conn, *out)
                input.trigger = command
                input.text_unstripped = match.group(2)
                input.text = input.text_unstripped.strip()

                dispatch(bot, input, plugin)

        # REGEXES
        for regex, plugin in bot.plugin_manager.regex_plugins:
            match = regex.search(inp.lastparam)
            if match:
                input = Input(bot, conn, *out)
                input.text = match

                dispatch(bot, input, plugin)
