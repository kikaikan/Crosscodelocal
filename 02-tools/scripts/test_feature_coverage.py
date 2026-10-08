"""feature_coverage.py 的登记扫描回归；只解析临时源码，不写仓库产物。"""
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feature_coverage as coverage


class ScanImplementationTests(unittest.TestCase):
    """@register 字面量/模块级常量解析与 READS 字典扫描的机械规则。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='crosscore-coverage-')
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def module(self, name, body):
        path = self.root / name
        path.write_text(textwrap.dedent(body), encoding='utf-8')
        return path

    def scan(self, *paths):
        return coverage.scan_implementations(list(paths))

    def line_of(self, path, needle):
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if line.startswith(needle):
                return number
        raise AssertionError('not found: ' + needle)

    def test_literal_decorator_records_file_line_and_function(self):
        path = self.module('literal.py', '''
            @register('DemoProto:Alpha')
            async def alpha(ctx, fields):
                return []
        ''')
        found = self.scan(path)
        self.assertEqual(set(found), {'DemoProto:Alpha'})
        entry = found['DemoProto:Alpha']
        self.assertEqual(entry['function'], 'alpha')
        self.assertEqual(entry['line'], self.line_of(path, 'async def alpha'))
        self.assertEqual(entry['file'], str(path.resolve()).replace('\\', '/'))

    def test_module_level_string_constant_is_resolved(self):
        path = self.module('constant.py', '''
            BETA = 'DemoProto:Beta'
            EXTRA = "DemoProto:Extra"

            @register(BETA)
            def beta(ctx, fields):
                return []

            @register(EXTRA)
            def extra(ctx, fields):
                return []
        ''')
        found = self.scan(path)
        self.assertEqual(sorted(found), ['DemoProto:Beta', 'DemoProto:Extra'])
        self.assertEqual(found['DemoProto:Beta']['function'], 'beta')
        self.assertEqual(found['DemoProto:Extra']['function'], 'extra')

    def test_constant_defined_after_the_decorator_still_resolves(self):
        path = self.module('ordered.py', '''
            @register(LATE)
            def late(ctx, fields):
                return []

            LATE = 'DemoProto:Late'
        ''')
        self.assertEqual(sorted(self.scan(path)), ['DemoProto:Late'])

    def test_derived_or_parameter_names_are_skipped_not_guessed(self):
        path = self.module('derived.py', '''
            BASE = 'DemoProto:'
            CONCAT = 'DemoProto:' + 'Concat'
            JOINED = f'DemoProto:Joined'
            ALIAS = BASE

            @register(CONCAT)
            def concat(ctx, fields):
                return []

            @register(JOINED)
            def joined(ctx, fields):
                return []

            @register(ALIAS)
            def alias(ctx, fields):
                return []

            def wire(table):
                for request in table:
                    @register(request)
                    def handler(ctx, fields):
                        return []
                return handler
        ''')
        self.assertEqual(self.scan(path), {})

    def test_readspec_dictionary_is_still_registered(self):
        path = self.module('reads.py', '''
            READS = {
                'DemoProto:ReadA': ReadSpec('DemoProto:ReadARet', {}),
                'DemoProto:ReadB': ReadSpec('DemoProto:ReadBRet', {}),
            }
        ''')
        found = self.scan(path)
        self.assertEqual(sorted(found), ['DemoProto:ReadA', 'DemoProto:ReadB'])
        self.assertEqual(found['DemoProto:ReadA']['function'], '_register_read')

    def test_duplicate_registration_still_raises(self):
        path = self.module('dup.py', '''
            DUP = 'DemoProto:Dup'

            @register('DemoProto:Dup')
            def first(ctx, fields):
                return []

            @register(DUP)
            def second(ctx, fields):
                return []
        ''')
        with self.assertRaises(ValueError):
            self.scan(path)

    def test_duplicate_between_reads_and_constant_raises(self):
        path = self.module('dup_reads.py', '''
            READS = {'DemoProto:Dup': ReadSpec('DemoProto:DupRet', {})}
            DUP = 'DemoProto:Dup'

            @register(DUP)
            def handler(ctx, fields):
                return []
        ''')
        with self.assertRaises(ValueError):
            self.scan(path)

    def test_non_string_constant_keeps_previous_behaviour(self):
        path = self.module('numeric.py', '''
            @register(7)
            def numeric(ctx, fields):
                return []
        ''')
        self.assertEqual(set(self.scan(path)), {7})


if __name__ == '__main__':
    unittest.main()
