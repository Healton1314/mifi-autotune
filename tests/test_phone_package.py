"""「手机安装包必须能被 Termux 直接跑」的测试。

背景：Windows 上工作区的 phone/*.sh 是 CRLF 时，zip 出去的脚本到了手机上
bash 会把行尾的 \\r 当命令的一部分，报的是
  install.sh: line 11: PREFIX: command not found
  install.sh: line 50: `case "$PKG" in': syntax error: unexpected token `in'
这种完全联想不到换行符的错。打包时就抹平，谁的工作区什么样都不影响。
"""

import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class TestPhonePackage(unittest.TestCase):
    def setUp(self):
        self.data = server.build_phone_package()
        self.zip = zipfile.ZipFile(io.BytesIO(self.data))

    def test_shell_scripts_are_lf(self):
        scripts = [n for n in self.zip.namelist() if n.endswith(".sh")]
        self.assertTrue(scripts, "安装包里居然没有 .sh")
        for name in scripts:
            body = self.zip.read(name)
            self.assertNotIn(b"\r", body, f"{name} 里有 CR，手机上会跑不起来")
            self.assertTrue(body.startswith(b"#!"), f"{name} 不像脚本")

    def test_install_script_checks_are_intact(self):
        """关键行没被换行符改坏：set -u 和 case 都得原样在。"""
        body = self.zip.read("mifi-autotune/phone/install.sh").decode("utf-8")
        self.assertIn("\nset -u\n", body)
        self.assertIn('case "$PKG" in\n', body)

    def test_no_host_secrets(self):
        """本机运行期数据不能混进包。"""
        names = set(self.zip.namelist())
        for bad in ("config.json", "state.json", "plan.json", ".panel-token"):
            self.assertNotIn(f"mifi-autotune/{bad}", names)

    def test_bat_passed_through_untouched(self):
        """.bat 是给 Windows 用的，打包时原样复制，不参与 LF 归一。"""
        bat = [n for n in self.zip.namelist() if n.endswith(".bat")]
        if not bat:
            self.skipTest("包内没有 .bat")
        for name in bat:
            # 不能用 str.removeprefix（3.9+），CI 要跑 3.8 —— 会直接
            # AttributeError: 'str' object has no attribute 'removeprefix'。
            rel = name[len("mifi-autotune/"):] if name.startswith("mifi-autotune/") else name
            src = ROOT / rel
            self.assertEqual(self.zip.read(name), src.read_bytes(),
                             f"{name} 被打包过程改动了")

    def test_source_conf_files_present(self):
        names = self.zip.namelist()
        self.assertIn("mifi-autotune/autotune.py", names)
        self.assertIn("mifi-autotune/phone/tips.txt", names)
        self.assertNotIn("mifi-autotune/tests/test_scoring.py", names)

    def test_output_log_has_a_path_the_pc_can_read(self):
        """输出要能复盘：优先落到 ~/storage/downloads（电脑数据线能读到），
        没给存储权限时退回 Termux 自己的目录，两种情况都不能崩。"""
        body = self.zip.read("mifi-autotune/phone/mifi.sh").decode("utf-8")
        self.assertIn('LOG="$DIR/last-run.log"', body)          # 兜底位置
        self.assertIn('for _d in "$HOME/storage/downloads"; do', body)   # 首选位置
        self.assertIn('LOG="$_d/mifi-last.log"', body)
        self.assertIn('tee -a "$LOG"', body)
        self.assertIn("run --guard", body)          # guard 之后要放 wake lock，不能 exec



if __name__ == "__main__":
    unittest.main()
