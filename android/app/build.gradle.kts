plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// 仓库根目录（android/ 的上一级）——Python 源码真身在这里，不在 android/ 里。
val repoRoot = rootProject.projectDir.parentFile
val stagedPythonDir = layout.projectDirectory.dir("src/main/python")

// 把「仓库根的 Python 源码」+「APK 专用桥接代码」合并成 Chaquopy 能吃的源集。
//
// 为什么要 stage 而不是直接把 sourceSets 指向仓库根：仓库根还躺着 .git、tests、
// 上百兆的 _apk 之类的构建产物，直接指过去会把整个目录扫一遍，慢且容易把不该
// 进包的东西（日志、记忆文件、令牌）一起打进去。
val stagePython = tasks.register<Copy>("stagePython") {
    description = "汇总 Python 源码到 Chaquopy 源集"
    group = "chaquopy"

    from(repoRoot) {
        include("*.py")
        include("drivers/**/*.py")
        include("web/**")
        exclude("**/__pycache__/**")
    }
    // APK 专属的桥接层（扫描结果注入、可写目录、面板启动），不进仓库根
    from("src/apkpy")

    into(stagedPythonDir)
    includeEmptyDirs = false
}

tasks.named("preBuild") { dependsOn(stagePython) }

android {
    namespace = "io.github.mifiautotune.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.mifiautotune.app"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "0.6.9"

        // 只要真机 ABI，而且只留 64 位。
        //
        // Python 3.12 起 Chaquopy 只提供 64 位运行时，加上 armeabi-v7a 会直接失败；
        // arm64 已覆盖近十年的安卓机，32 位老机型不在支持范围内。
        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    buildTypes {
        getByName("debug") {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

// Kotlin DSL 里这个扩展叫 chaquopy，不叫 python（Groovy 才叫 python）。
// 写成 python { } 的后果是配置阶段就崩，报的是 Kotlin 脚本求值异常，
// 完全看不出跟 Python 有关 —— 第一次构建就是这么失败的。
chaquopy {
    defaultConfig {
        // 必须与构建机的 python3 次版本一致，否则 Chaquopy 编译 .pyc 时会拒绝。
        // GitHub Actions 的 ubuntu-latest 自带 3.12。
        version = "3.12"
        buildPython("python3")
        // 本项目纯标准库，没有第三方包要装 —— 这正是它能塞进 APK 的根本原因
        pip {}
    }
}
