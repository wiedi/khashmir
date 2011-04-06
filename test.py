## Copyright 2002-2003 Andrew Loewenstern, All Rights Reserved
# see LICENSE.txt for license information

import unittest

import ktable, khashmir
import khash, node, knode
import actions
import test_airhook
import test_krpc

tests = unittest.defaultTestLoader.loadTestsFromNames(['khash', 'node', 'knode', 'actions',  'ktable', 'test_airhook', 'test_krpc'])
result = unittest.TextTestRunner().run(tests)
