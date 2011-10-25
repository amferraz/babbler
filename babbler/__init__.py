"""
A Twitter bot that polls an RSS feed and posts the feed's titles at
random intervals as tweets, extracting words from the titles to use
as hashtags.
"""

from __future__ import with_statement
from cPickle import dump, load
import logging
from math import ceil
from optparse import OptionParser
from os import getcwd, kill, remove
from os.path import dirname, join
from time import sleep, time

from daemon import daemonize
from feedparser import parse
from twitter import Api, TwitterError


__version__ = "0.1"

DATA_PATH = join(getcwd(), "babbler.data")
PID_PATH = join(getcwd(), "babbler.pid")
TWEET_MAX_LEN = 140
QUEUE_SLICE = .3  # Fraction of the "todo" queue flushed during each run.


def wordfile(filename):
    """
    Returns a set word list from a file.
    Used for dictionary and stopwords.
    """
    with open(join(dirname(__file__), "wordfiles", filename)) as f:
        return set([s.strip() for s in f])


def save(dry_run=False):
    """
    Persists the data file to disk.
    """
    if not dry_run:
        with open(DATA_PATH, "wb") as f:
            dump(data, f)


def configure():
    """
    Handles command-line arg parsing and loading of options and data.
    """
    global options, data, api, dictionary, stopwords

    parser = OptionParser(usage="usage: %prog [options]")
    parser.add_option("--hashtag-length-min", dest="hashtag_len_min",
                      default=3,
                      help="Minimum length of a hashtag")
    parser.add_option("--delay", dest="delay",
                      default=600,
                      help="Number of seconds between RSS feed requests")
    parser.add_option("--ignore", dest="ignore",
                      default="",
                      help="Comma separated strings for ignoring feed entries")
    parser.add_option("--loglevel", dest="loglevel",
                      default="info", choices=("error", "info", "debug"),
                      help="Level of information printed")
    parser.add_option("--dry-run", dest="dry_run", action="store_true",
                      default=False,
                      help="Fake run without posting any tweets")
    parser.add_option("--daemonize", dest="daemonize", action="store_true",
                      default=False,
                      help="Run as a daemon")
    parser.add_option("--kill", dest="kill", action="store_true",
                      default=False,
                      help="Kill a previously started daemon")
    parser.add_option("--DESTROY", dest="destroy", action="store_true",
                      default=False,
                      help="Deletes all saved data and tweets from Twitter")
    parser.add_option("--feed-url", dest="feed_url",
                      help="RSS Feed URL")
    parser.add_option("--consumer-key", dest="consumer_key",
                      help="Twitter Consumer Key")
    parser.add_option("--consumer-secret", dest="consumer_secret",
                      help="Twitter Consumer Secret")
    parser.add_option("--access-token-key", dest="access_token_key",
                      help="Twitter Access Token Key")
    parser.add_option("--access-token-secret", dest="access_token_secret",
                      help="Twitter Access Token Secret")
    (parsed_options, args) = parser.parse_args()

    try:
        # Try and load a previously saved data file.
        with open(DATA_PATH, "rb") as f:
            data = load(f)
    except IOError:
        # If no data file exists, prompt the user for the required options
        # and create the data file, persisting the entered options.
        print
        print "Initial setup."
        print "All data will be saved to '%s'" % DATA_PATH
        print "Press CTRL C to abort."
        print
        options = {}
        for option in parser.option_list:
            if option.dest is not None:
                value = getattr(parsed_options, option.dest)
                if value is None:
                    value = raw_input("Please enter '%s': " % option.help)
                options[option.dest] = value
        data = {"options": options, "todo": [], "done": set()}
    else:
        # Override any previously saved options with any values
        # provided via command line.
        for option in parser.option_list:
            if option.dest is not None:
                value = getattr(parsed_options, option.dest)
                if value is not None:
                    data["options"][option.dest] = value
        options = data["options"]

    # Save parsed options.
    save()

    # Set up the Twitter API object.
    api = Api(**dict([(k, v) for k, v in options.items()
                      if k.split("_")[0] in ("consumer", "access")]))

    # Set up word files.
    dictionary = wordfile("dictionary.txt")
    stopwords = wordfile("stopwords.txt")

    # Set up logging.
    logging.basicConfig(format="%(asctime)s %(message)s")
    logging.getLogger().setLevel(getattr(logging, options["loglevel"].upper()))

    return parsed_options


def destroy():
    """
    Destroys persisted data file and deletes all tweets from Twitter
    when the --DESTROY option is given.
    """
    print
    print "WARNING: You have specified the --DESTROY option."
    print "All tweets will be deleted from your account."
    if raw_input("Enter 'y' to continue. ").strip().lower() == "y":
        print "Deleting all data and tweets."
        try:
            remove(DATA_PATH)
        except OSError:
            pass
        while True:
            tweets = api.GetUserTimeline()
            if not tweets:
                break
            for tweet in tweets:
                try:
                    api.DestroyStatus(tweet.id)
                except TwitterError:
                    pass
        print "Done."
    else:
        print "--DESTROY aborted"


def get_new_entries():
    """
    Loads the RSS feed in reverse order and return new entries.
    """
    entries = []
    feed = parse(options["feed_url"])
    try:
        logging.error("Feed error: %s" % feed["bozo_exception"])
    except KeyError:
        pass
    saved = set([t["id"] for t in data["todo"]]) | data["done"]
    for entry in reversed(feed.entries):
        if entry["id"] not in saved:
            # Ignore entries that match an ignore string, can't fit
            # into a tweet, or are already in "todo" or "done".
            ignored = [s for s in options["ignore"].split(",")
                       if s and s.lower() in entry["title"].lower()]
            if ignored:
                logging.debug("Ignore strings (%s) found in: %s" %
                              (", ".join(ignored), entry["title"]))
                data["done"].add(entry["id"])
            elif len(entry["title"]) > TWEET_MAX_LEN:
                logging.debug("Entry too long: %s" % entry["title"])
                data["done"].add(entry["id"])
            else:
                entries.append({"id": entry["id"], "title": entry["title"]})
    return entries


def possible_hashtags_for_index(words, i):
    """
    Returns up to 4 possible hashtags - all combinations of the next
    and previous words for the given index. If the word has a
    possessive apostrophe, run again using the singular form.
    """
    valid_prev = i > 0 and words[i - 1] not in stopwords
    valid_next = i < len(words) - 1 and words[i + 1] not in stopwords
    base_words = [words[i]]
    if words[i].endswith("'s"):
        # Singular for possessive.
        base_words.append(words[i][:-2])
    possible_hashtags = []
    for word in base_words:
        possible_hashtags.append(word)
        if valid_prev:
            # Combined with previous word.
            possible_hashtags.append(words[i - 1] + word)
        if valid_next:
            # Combined with next word.
            possible_hashtags.append(word + words[i + 1])
        if valid_prev and valid_next:
            # Combined with previous and next words.
            possible_hashtags.append(words[i - 1] + word + words[i + 1])
    # Remove apostophes.
    return [t.replace("'", "") for t in possible_hashtags]


def best_hashtag_with_score(possible_hashtags):
    """
    Given possible hashtags, calculates a score for each based on the
    time since epoch of each search result for the hashtag, and returns
    the highest scoring hashtag/score pair.
    """
    best_hashtag = None
    highest_score = 0
    for hashtag in possible_hashtags:
        if len(hashtag) >= options["hashtag_len_min"]:
            try:
                results = api.GetSearch("#" + hashtag)
            except TwitterError, e:
                logging.error("Twitter error: %s" % e)
            else:
                score = sum([t.created_at_in_seconds for t in results])
                logging.debug("Score for '%s': %s" % (hashtag, score))
                if score > highest_score:
                    highest_score = score
                    best_hashtag = hashtag
    return best_hashtag, highest_score


def tweet_with_hashtags(tweet):
    """
    Parses hashtags from the given tweet and adds them to the
    returned tweet.

    Steps:

    1) Go through every word in the tweet and if non-dictionary and
       non-numeric, create up to 4 possible hashtags from it, the word
       combined with the previous word, the next word, both previous
       and next words together, and the word itself. Only use previous
       and next words that aren't stopwords.
    2) Ignore all the possible hashtags from the word if any of
       them have already been added as hashtags, eg via the previous
       or next word iteration, or a duplicate word.
    3) Search for the possible hashtags via the API, giving each a
       score based on the sum of the seconds since epoch for each
       search result, and pick the highest scoring hashtag to use from
       the possibilites for that word.
    4) Sort the chosen hashtags found for all words by score, and add
       as many as possible to the tweet within its length limit.
    """

    logging.debug("Getting hashtags for: %s" % tweet)
    # String for word list - treat dashes and slashes as separators.
    cleaned = tweet.lower().replace("-", " ").replace("/", " ")
    # Initial list of alphanumeric words.
    words = "".join([c for c in cleaned if c.isalnum() or c in "' "]).split()
    # All hashtags mapped to scores.
    hashtags = {}
    for i, word in enumerate(words):
        word = word.replace("'", "")
        if not (word.isdigit() or word in dictionary):
            possible_hashtags = possible_hashtags_for_index(words, i)
            logging.debug("Possible hashtags for the word '%s': %s" %
                          (word, ", ".join(possible_hashtags)))
            # Check none of the possibilities have been used.
            used = [t for t in possible_hashtags if t in hashtags.keys()]
            if used:
                logging.debug("Possible hashtags already used")
            else:
                hashtag, score = best_hashtag_with_score(possible_hashtags)
                if hashtag is not None:
                    hashtags[hashtag] = score

    # Sort hashtags by score and add to tweet.
    hashtags = sorted(hashtags.keys(), key=lambda k: hashtags[k], reverse=True)
    logging.debug("Hashtags chosen: %s" % (", ".join(hashtags)
                                           if hashtags else "None"))
    for hashtag in hashtags:
        hashtag = " #" + hashtag
        if len(tweet + hashtag) <= TWEET_MAX_LEN:
            tweet += hashtag
    return tweet


def run(dry_run):
    """
    Main event loop that gets the entries from the feed and goes through
    them, oldest first, adding them to the "todo" queue. Then takes the
    first from the queue and posts it to Twitter. Finally pauses for the
    amount of time estimated to flush the "todo" queue by the time the
    feed is requested again.
    """
    last_feed_time = 0
    while True:
        # Get new entries and save the data file if new entries found
        # if the delay period has elapsed.
        if ((last_feed_time + int(options["delay"])) - time()) <= 0:
            last_feed_time = time()
            new_entries = get_new_entries()
            logging.debug("New queued entries: %s" % len(new_entries))
            if new_entries:
                data["todo"].extend(new_entries)
                save(dry_run=dry_run)
            # Update the time to sleep - use the delay option unless
            # there are items in the "todo" queue, otherwise set the
            # delay to consume the portion of the queue size defined
            # by QUEUE_SLICE before the next feed request.
            delay = int(options["delay"])
            if data["todo"] > QUEUE_SLICE * 10:
                delay = int(delay / ceil(len(data["todo"]) * QUEUE_SLICE))
        # Process the first entry in the "todo" list.
        if data["todo"]:
            logging.debug("Total queued entries: %s" % len(data["todo"]))
            tweet = tweet_with_hashtags(data["todo"][0]["title"])
            # Post to Twitter.
            done = True
            try:
                if not dry_run:
                    api.PostUpdate(tweet)
            except TwitterError, e:
                logging.error("Twitter error: %s" % e)
                # Mark the entry as done if it's a duplicate.
                done = str(e) == "Status is a duplicate."
            if done:
                logging.info("Tweeted: %s" % tweet)
                # Move the entry from "todo" to "done" and save.
                data["done"].add(data["todo"].pop(0)["id"])
                save(dry_run=dry_run)
        logging.debug("Pausing for %s seconds" % delay)
        sleep(delay)


def kill_daemon():
    """
    Try to stop a previously started daemon.
    """
    try:
        with open(PID_PATH) as f:
            kill(int(f.read()), 9)
        remove(PID_PATH)
    except (IOError, OSError):
        return False
    return True


def main():
    """
    Main entry point for program.
    """
    parsed_options = configure()
    if parsed_options.destroy:
        # Reset all data and delete tweets if specified.
        destroy()
    elif parsed_options.kill:
        # Kill a previously started daemon.
        if kill_daemon():
            print "Daemon killed"
        else:
            print "Couldn't kill daemon"
    elif parsed_options.daemonize:
        # Start a new daemon.
        kill_daemon()
        daemonize(PID_PATH)
        run(parsed_options.dry_run)
        print "Daemon started"
    else:
        # Start in the foreground.
        try:
            run(parsed_options.dry_run)
        except KeyboardInterrupt:
            print
            print "Quitting"


if __name__ == "__main__":
    main()