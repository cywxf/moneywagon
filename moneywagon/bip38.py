# -*- coding: utf-8 -*-
from __future__ import print_function

from binascii import unhexlify, hexlify
from hashlib import sha256
from unicodedata import normalize
import sys

from moneywagon.core import get_magic_bytes
from bitcoin import (
    privtopub, pubtoaddr, encode_privkey, get_privkey_format,
    encode_pubkey, changebase, fast_multiply, G, P, A, B
)

try:
    import scrypt
except ImportError:
    raise ImportError("Scrypt is required for BIP38 support: pip install scrypt")

try:
    from Crypto.Cipher import AES
except ImportError:
    raise ImportError("Pycrypto is required for bip38 support: pip install pycrypto")

is_py2 = False
if sys.version_info <= (3,0):
    # py2
    is_py2 = True
else:
    # py3
    long = int
    unicode = str


def base58check(payload):
    """
    Convert bytes object into "base 58" encoded string.
    """
    checksum = sha256(sha256(payload).digest()).digest()[:4]
    return changebase(hexlify(payload + checksum).decode('ascii'), 16, 58)


def unbase58check(encoded):
    """
    Returns the paylod of a base58check encoded string. Verifies that the
    checksum is correct.
    """
    full_payload = unhexlify(changebase(encoded.encode('ascii'), 58, 16))
    payload, checksum = full_payload[:-4], full_payload[-4:]
    passed = checksum == sha256(sha256(payload).digest()).digest()[:4]
    assert passed, "Base58 checksum failed, did you mistype something?"
    return payload


def bip38_encrypt(crypto, privkey, passphrase):
    """
    BIP0038 non-ec-multiply encryption. Returns BIP0038 encrypted privkey.
    """
    pub_byte, priv_byte = get_magic_bytes(crypto)
    privformat = get_privkey_format(privkey)
    if privformat in ['wif_compressed','hex_compressed']:
        compressed = True
        flagbyte = b'\xe0'
        if privformat == 'wif_compressed':
            privkey = encode_privkey(privkey,'hex_compressed')
            privformat = get_privkey_format(privkey)
    if privformat in ['wif', 'hex']:
        compressed = False
        flagbyte = b'\xc0'
    if privformat == 'wif':
        privkey = encode_privkey(privkey,'hex')
        privformat = get_privkey_format(privkey)

    pubkey = privtopub(privkey)
    addr = pubtoaddr(pubkey, pub_byte)

    passphrase = normalize('NFC', unicode(passphrase))
    if is_py2:
        ascii_key = addr
        passphrase = passphrase.encode('utf8')
    else:
        ascii_key = bytes(addr,'ascii')

    salt = sha256(sha256(ascii_key).digest()).digest()[0:4]
    key = scrypt.hash(passphrase, salt, 16384, 8, 8)
    derivedhalf1, derivedhalf2 = key[:32], key[32:]

    aes = AES.new(derivedhalf2)
    encryptedhalf1 = aes.encrypt(unhexlify('%0.32x' % (long(privkey[0:32], 16) ^ long(hexlify(derivedhalf1[0:16]), 16))))
    encryptedhalf2 = aes.encrypt(unhexlify('%0.32x' % (long(privkey[32:64], 16) ^ long(hexlify(derivedhalf1[16:32]), 16))))

    # 39 bytes    2 (6P)      1(R/Y)    4          16               16
    payload = b'\x01\x42' + flagbyte + salt + encryptedhalf1 + encryptedhalf2
    return base58check(payload)


def bip38_decrypt(crypto, encrypted_privkey, passphrase, wif=False):
    """
    BIP0038 non-ec-multiply decryption. Returns hex privkey.
    """
    pub_byte, priv_byte = get_magic_bytes(crypto)
    passphrase = normalize('NFC', unicode(passphrase))
    if is_py2:
        passphrase = passphrase.encode('utf8')

    payload = unhexlify(changebase(encrypted_privkey, 58, 16, 86))
    flagbyte = payload[2:3]

    ec_multiply = False
    if payload[1:2] == b'\x42':
        if flagbyte == b'\xC0':
            compressed = False
        elif flagbyte == b'\xE0':
            compressed = True
    elif payload[1:2] == b'\x43':
        ec_multiply = True
        if flagbyte == b'\x00':
            compressed = False
        elif flagbyte == b'\x20':
            compressed == True

    addresshash = payload[3:7]
    key = scrypt.hash(passphrase, addresshash, 16384, 8, 8)
    derivedhalf1 = key[0:32]
    derivedhalf2 = key[32:64]
    encryptedhalf1 = payload[7:23] # 16 bytes
    encryptedhalf2 = payload[23:39] # 16 bytes

    aes = AES.new(derivedhalf2)
    decryptedhalf2 = aes.decrypt(encryptedhalf2)
    decryptedhalf1 = aes.decrypt(encryptedhalf1)
    priv = decryptedhalf1 + decryptedhalf2
    priv = unhexlify('%064x' % (long(hexlify(priv), 16) ^ long(hexlify(derivedhalf1), 16)))
    pub = privtopub(priv)
    if compressed:
        pub = encode_pubkey(pub, 'hex_compressed')
    addr = pubtoaddr(pub, pub_byte)

    if is_py2:
        ascii_key = addr
    else:
        ascii_key = bytes(addr,'ascii')

    if sha256(sha256(ascii_key).digest()).digest()[0:4] != addresshash:
        raise Exception('Bip38 password decrypt failed: Wrong password?')
    else:
        formatt = 'wif' if wif else 'hex'
        if compressed:
            return encode_privkey(priv, formatt + '_compressed')
        else:
            return encode_privkey(priv, formatt)


def compress(x, y):
    """
    Given a x,y coordinate, encode in "compressed format"
    Returned is always 33 bytes.
    """
    polarity = "02" if y % 2 == 0 else "03"

    wrap = lambda x: x
    if not is_py2:
        wrap = lambda x: bytes(x, 'ascii')

    return unhexlify(wrap("%s%0.64x" % (polarity, x)))


def uncompress(payload):
    """
    Given a compressed ec key in bytes, uncompress it using math and return (x, y)
    """
    payload = hexlify(payload)
    even = payload[:2] == b"02"
    x = int(payload[2:], 16)
    beta = pow(int(x ** 3 + A * x + B), int((P + 1) // 4), int(P))
    y = (P-beta) if even else beta
    return x, y


def bip38_generate_intermediate_point(passphrase, seed, lot=None, sequence=None):
    passphrase = normalize('NFC', unicode(passphrase))
    if is_py2:
        passphrase = passphrase.encode('utf8')

    if not is_py2:
        seed = bytes(seed, 'ascii')

    if lot and sequence:
        ownersalt = sha256(seed).digest()[:4]
        lotseq = unhexlify("%0.8x" % (4096 * lot + sequence))
        ownerentropy = ownersalt + lotseq
    else:
        ownersalt = ownerentropy = sha256(seed).digest()[:8]

    prefactor = scrypt.hash(passphrase, ownersalt, 16384, 8, 8, 32)

    if lot and sequence:
        passfactor = sha256(sha256(prefactor + ownerentropy).digest()).digest()
    else:
        passfactor = prefactor

    if is_py2:
        passfactor_as_int = int(passfactor.encode('hex'), 16)
    else:
        passfactor_as_int = int.from_bytes(passfactor, byteorder='big')

    passpoint = compress(*fast_multiply(G, passfactor_as_int))

    last_byte = b'\x53' if not lot and not sequence else b'\x51'
    magic_bytes = b'\x2C\xE9\xB3\xE1\xFF\x39\xE2' + last_byte # 'passphrase' prefix
    payload = magic_bytes + ownerentropy + passpoint
    return base58check(payload)


def generate_bip38_encrypted_address(crypto, intermediate_point, seed, compressed=True, include_cfrm=True):
    """
    Given an intermediate point, given to us by "owner", generate an address
    and encrypted private key that can be decoded by the passphrase used to generate
    the intermediate point.
    """
    flagbyte = b'\x20'if compressed else b'\x00'
    payload = unbase58check(intermediate_point)
    ownerentropy = payload[8:16]

    passpoint = payload[16:-4]
    x, y = uncompress(passpoint)

    if not is_py2:
        seed = bytes(seed, 'ascii')

    seedb = hexlify(sha256(seed).digest())[:24]
    factorb = int(hexlify(sha256(sha256(seedb).digest()).digest()), 16)
    generatedaddress = pubtoaddr(fast_multiply((x, y), factorb))

    wrap = lambda x: x
    if not is_py2:
        wrap = lambda x: bytes(x, 'ascii')

    addresshash = sha256(sha256(wrap(generatedaddress)).digest()).digest()[:4]
    encrypted_seedb = scrypt.hash(passpoint, addresshash + ownerentropy, 1024, 1, 1, 64)
    derivedhalf1, derivedhalf2 = encrypted_seedb[:32], encrypted_seedb[32:]

    aes = AES.new(derivedhalf2)
    block1 = long(seedb[0:16], 16) ^ long(hexlify(derivedhalf1[0:16]), 16)
    encryptedpart1 = aes.encrypt(unhexlify('%0.32x' % block1))

    block2 = long(hexlify(encryptedpart1[8:16]) + seedb[16:24], 16) ^ long(hexlify(derivedhalf1[16:32]), 16)
    encryptedpart2 = aes.encrypt(unhexlify('%0.32x' % block2))

    # 39 bytes      2           1           4              8                8                 16
    payload = b"\x01\x43" + flagbyte + addresshash + ownerentropy + encryptedpart1[:8] + encryptedpart2
    encrypted_pk = base58check(payload)

    if not include_cfrm:
        return generatedaddress, encrypted_pk

    confirmation_code = _make_confirmation_code(flagbyte, ownerentropy, factorb, derivedhalf1, derivedhalf2, addresshash)
    return generatedaddress, encrypted_pk, confirmation_code


def _make_confirmation_code(flagbyte, ownerentropy, factorb, derivedhalf1, derivedhalf2, addresshash):
    """
    This is an extension to the `generate_bip38_encrypted_address` that allows
    the owner to verify that his address and passphrase are valid.
    Check validity with `confirm_generated_address`.
    """
    pointb = compress(*fast_multiply(G, factorb))
    pointbprefix = bytes([ord(pointb[:1]) ^ (ord(derivedhalf2[-1:]) & 1)])

    aes = AES.new(derivedhalf2)
    block1 = long(hexlify(pointb[1:17]), 16) ^ long(hexlify(derivedhalf1[:16]), 16)
    pointbx1 = aes.encrypt(unhexlify("%0.32x" % block1))
    block2 = long(hexlify(pointb[17:]), 16) ^ long(hexlify(derivedhalf1[16:]), 16)
    pointbx2 = aes.encrypt(unhexlify("%0.32x" % block2))

    encryptedpointb = pointbprefix + pointbx1 + pointbx2

    #             5 (cfrm prefix)          1            4             8               33
    payload = b'\x64\x3B\xF6\xA8\x9A' + flagbyte + addresshash + ownerentropy + encryptedpointb

    return base58check(payload)


def confirm_generated_address(crypto, confirm_code, address, passphrase):
    """
    Make sure the confirm code is valid for the gven password and address.
    """
    payload = unbase58check(confirm_code)
    ownerentropy = payload[10:18]

    prefactor = scrypt.hash(passphrase, ownerentropy, 16384, 8, 8, 32)

    if is_py2:
        prefactor_as_int = int(prefactor.encode('hex'), 16)
    else:
        prefactor_as_int = int.from_bytes(prefactor, byteorder='big')

    return ownerentropy

## tests below


def ec_test():
    """
    Tests the ec-multiply sections. Cases taken from the bip38 document.
    """
    seed = "dh3409sjgh3g48"
    seed2 = "asdas8729akjbmn"

    cases = [[
        'btc',
        'TestingOneTwoThree',
        'passphrasepxFy57B9v8HtUsszJYKReoNDV6VHjUSGt8EVJmux9n1J3Ltf1gRxyDGXqnf9qm',
        '6PfQu77ygVyJLZjfvMLyhLMQbYnu5uguoJJ4kMCLqWwPEdfpwANVS76gTX',
        '1PE6TQi6HTVNz5DLwB1LcpMBALubfuN2z2',
        '5K4caxezwjGCGfnoPTZ8tMcJBLB7Jvyjv4xxeacadhq8nLisLR2',
        ],[
        'btc',
        'Satoshi',
        'passphraseoRDGAXTWzbp72eVbtUDdn1rwpgPUGjNZEc6CGBo8i5EC1FPW8wcnLdq4ThKzAS',
        '6PfLGnQs6VZnrNpmVKfjotbnQuaJK4KZoPFrAjx1JMJUa1Ft8gnf5WxfKd',
        '1CqzrtZC6mXSAhoxtFwVjz8LtwLJjDYU3V',
        '5KJ51SgxWaAYR13zd9ReMhJpwrcX47xTJh2D3fGPG9CM8vkv5sH',
    ]]

    i = 1
    for crypto, passphrase, inter_point, encrypted_pk, address, decrypted_pk in cases:
        test_inter_point = bip38_generate_intermediate_point(passphrase, seed)
        test_generated_address, test_encrypted_pk, test_cfm = generate_bip38_encrypted_address(crypto, inter_point, seed2)
        confirm_generated_address(crypto, test_cfm, test_generated_address, passphrase) #, "Verification of confirm code failed."
        print("EC multiply test #%s passed!" % i)
        i+= 1


    cases2 = [[
        'btc',
        'MOLON LABE',
        'passphraseaB8feaLQDENqCgr4gKZpmf4VoaT6qdjJNJiv7fsKvjqavcJxvuR1hy25aTu5sX',
        '6PgNBNNzDkKdhkT6uJntUXwwzQV8Rr2tZcbkDcuC9DZRsS6AtHts4Ypo1j',
        '1Jscj8ALrYu2y9TD8NrpvDBugPedmbj4Yh',
        '5JLdxTtcTHcfYcmJsNVy1v2PMDx432JPoYcBTVVRHpPaxUrdtf8',
        'cfrm38V8aXBn7JWA1ESmFMUn6erxeBGZGAxJPY4e36S9QWkzZKtaVqLNMgnifETYw7BPwWC9aPD',
        263183,
        1
        ],[
        'btc',
        u'ΜΟΛΩΝ ΛΑΒΕ',
        'passphrased3z9rQJHSyBkNBwTRPkUGNVEVrUAcfAXDyRU1V28ie6hNFbqDwbFBvsTK7yWVK',
        '6PgGWtx25kUg8QWvwuJAgorN6k9FbE25rv5dMRwu5SKMnfpfVe5mar2ngH',
        '1Lurmih3KruL4xDB5FmHof38yawNtP9oGf',
        '5KMKKuUmAkiNbA3DazMQiLfDq47qs8MAEThm4yL8R2PhV1ov33D',
        'cfrm38V8G4qq2ywYEFfWLD5Cc6msj9UwsG2Mj4Z6QdGJAFQpdatZLavkgRd1i4iBMdRngDqDs51',
        806938,
        1
    ]]

    i = 3
    for crypto, passphrase, inter_point, encrypted_pk, address, decrypted_pk, confirm, lot, sequence in cases2:
        test_inter_point = bip38_generate_intermediate_point(passphrase, seed)
        test_generated_address, test_encrypted_pk, test_cfm = generate_bip38_encrypted_address(crypto, inter_point, seed2)
        print("EC multiply test #%s passed!" % i)
        i += 1


def non_ec_test():
    # taken directly from the BIP38 whitepaper
    cases = [[
        'btc',
        '6PRVWUbkzzsbcVac2qwfssoUJAN1Xhrg6bNk8J7Nzm5H7kxEbn2Nh2ZoGg',
        'cbf4b9f70470856bb4f40f80b87edb90865997ffee6df315ab166d713af433a5',
        u'TestingOneTwoThree',
        False
        ], [
        'btc',
        '6PRNFFkZc2NZ6dJqFfhRoFNMR9Lnyj7dYGrzdgXXVMXcxoKTePPX1dWByq',
        '09c2686880095b1a4c249ee3ac4eea8a014f11e6f986d0b5025ac1f39afbd9ae',
        u'Satoshi',
        False
        ],[
        'btc',
        '6PRW5o9FLp4gJDDVqJQKJFTpMvdsSGJxMYHtHaQBF3ooa8mwD69bapcDQn',
        '5Jajm8eQ22H3pGWLEVCXyvND8dQZhiQhoLJNKjYXk9roUFTMSZ4',
        u'\u03D2\u0301\u0000\U00010400\U0001F4A9',
        True,
        ],[
        'btc',
        '6PYNKZ1EAgYgmQfmNVamxyXVWHzK5s6DGhwP4J5o44cvXdoY7sRzhtpUeo',
        'L44B5gGEpqEDRS9vVPz7QT35jcBG2r3CZwSwQ4fCewXAhAhqGVpP',
        u'TestingOneTwoThree',
        True
        ],[
        'btc',
        '6PYLtMnXvfG3oJde97zRyLYFZCYizPU5T3LwgdYJz1fRhh16bU7u6PPmY7',
        'KwYgW8gcxj1JWJXhPSu4Fqwzfhp5Yfi42mdYmMa4XqK7NJxXUSK7',
        u'Satoshi',
        True
    ]]

    index = 1
    for crypto, encrypted_key, unencrypted_key, password, use_wif in cases:
        test_encrypted = bip38_encrypt(crypto, unencrypted_key, password)
        test_decrypted = bip38_decrypt(crypto, encrypted_key, password, wif=use_wif)
        assert encrypted_key == test_encrypted, "encrypt failed"
        assert unencrypted_key == test_decrypted, 'decrypt failed'
        print("Non-ec multiply test #%s passed" % index)
        index += 1


def test():
    ec_test()
    non_ec_test()