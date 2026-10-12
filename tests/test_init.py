"""init must read real apps' build files: Kotlin DSL or Groovy, with or without product flavors."""

from droid_monkey.init_cmd import _ID, default_variant
import re

GROOVY_FLAVORS = """
android {
    defaultConfig {
        applicationId 'org.wikipedia'
    }
    buildTypes {
        debug { minifyEnabled false }
        release { minifyEnabled true }
    }
    productFlavors {
        prod { versionName 'r' }
        dev {
            isDefault = true
            applicationIdSuffix '.dev'
        }
        alpha { applicationIdSuffix '.alpha' }
    }
}
"""

KTS_PLAIN = """
android {
    defaultConfig { applicationId = "com.example.app" }
    buildTypes {
        getByName("release") { isMinifyEnabled = true }
        create("debug") { applicationIdSuffix = ".debug" }
    }
}
"""


def test_groovy_default_flavor_and_suffix():
    assert re.search(r"\bapplicationId" + _ID, GROOVY_FLAVORS).group(1) == "org.wikipedia"
    assert default_variant(GROOVY_FLAVORS) == ("devDebug", ".dev")


def test_kotlin_dsl_without_flavors_keeps_the_debug_suffix():
    assert re.search(r"\bapplicationId" + _ID, KTS_PLAIN).group(1) == "com.example.app"
    assert default_variant(KTS_PLAIN) == ("debug", ".debug")


def test_first_flavor_when_none_is_default():
    text = GROOVY_FLAVORS.replace("isDefault = true", "")
    assert default_variant(text) == ("prodDebug", "")
