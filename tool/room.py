"""Room words: a rotating word shown in the invigilated room that students type to prove they are there."""

import hashlib
import hmac
import secrets
import time

ROTATION_SECONDS = 20
CHECK_SECONDS = 10  # time a student has to type the word in a spot check
CHECK_GRACE_SECONDS = 3

# Short, common, concrete words: easy to read from the back of a room and to
# spell, with no British/American variants, homophones or plurals.
WORDS = """
acorn actor adult agent album alarm alley amber angle ankle apple apron arrow attic
badge bagel baker banjo barn basin basket beach beard beaver bench berry bicycle
blanket blossom border bottle bracket branch bread brick bridge broom brush bubble
bucket buffalo bundle butter button cabin cable cactus camel candle canoe canyon
carpet carrot castle cattle cellar chalk chapel cheese cherry chess chimney cinema
circle circus cliff clock cloud clover coast cobweb coconut coffee comet copper
cotton cousin crayon cricket crown crystal cupboard curtain cushion daisy dancer
desert diamond dinner doctor dolphin donkey dragon drawer dream drum eagle easel
echo elbow engine fabric falcon feather fence ferry field finger flame flute forest
fossil fountain fox frog fruit galaxy garden garlic giant ginger glacier glove
goat gravel guitar hammer harbour harvest helmet heron hockey honey hotel island
jacket jelly jigsaw journey jungle kayak kettle kitten ladder lantern lemon letter
lettuce library lily lizard lobster locket magnet mango maple marble market meadow
melon mirror monkey mountain muffin museum needle nest noodle oasis ocean office
olive onion orange orchard otter owl paddle palace panda paper parrot pasta peach
peanut pebble pencil pepper piano pickle pigeon pillow pirate planet plum pocket
pony potato puddle pumpkin puppet puzzle quarry rabbit radio railway rainbow raven
ribbon river robot rocket saddle salmon sandal satchel saucer scarf seagull shadow
shell shovel silver sketch sledge snail spider spoon squirrel stable statue stone
storm sugar summit sunset swan table teapot temple tiger timber toast tomato
tortoise towel tractor trumpet tulip tunnel turnip umbrella valley velvet violin
volcano wagon walnut walrus window winter wizard wolf yacht zebra
anchor beetle blender bonnet canal cargo cedar cobble compass cradle dagger dune
emerald fiddle gadget gazelle goblet gondola granite hazel hedge igloo iron ivory
jasmine kernel kite lagoon lagoon lemur linen lotus mallet meteor mitten moose
nectar oyster pantry pearl pelican petal pine plaster quill raft reef rhubarb
rubble saffron scooter sequin shrimp sparrow spinach sponge stirrup
straw sundial tadpole thimble thistle toffee trolley truffle tuba waffle
walrus wheel whistle willow yogurt zipper
""".split()
WORDS = sorted(set(WORDS))


def new_secret():
    return secrets.token_hex(16)


def _window(now=None):
    return int((now if now is not None else time.time()) // ROTATION_SECONDS)


def word_for_window(secret, window):
    digest = hmac.new(secret.encode(), str(window).encode(), hashlib.sha256).digest()
    index = int.from_bytes(digest[:8], "big") % len(WORDS)
    # Never show the same word twice in a row.
    previous = int.from_bytes(hmac.new(secret.encode(), str(window - 1).encode(), hashlib.sha256).digest()[:8], "big") % len(WORDS)
    if index == previous:
        index = (index + 1) % len(WORDS)
    return WORDS[index]


def current_word(secret, now=None):
    """The word on screen now and the seconds until it changes."""
    now = now if now is not None else time.time()
    return word_for_window(secret, _window(now)), ROTATION_SECONDS - (now % ROTATION_SECONDS)


def _close_enough(typed, word):
    """Exact match, or one typo (insertion, deletion or substitution) for words of 5+ letters."""
    if typed == word:
        return True
    if len(word) < 5 or abs(len(typed) - len(word)) > 1:
        return False
    previous = list(range(len(word) + 1))
    for i, a in enumerate(typed, 1):
        row = [i]
        for j, b in enumerate(word, 1):
            row.append(min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = row
    return previous[-1] <= 1


def check(secret, typed, now=None):
    """Accept the current word or the one just before it (in case it changed while typing)."""
    typed = "".join(str(typed or "").lower().split())
    if not typed:
        return False
    window = _window(now)
    return any(_close_enough(typed, word_for_window(secret, w)) for w in (window, window - 1))
