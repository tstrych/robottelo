"""Global Configurations for py.test runner"""

pytest_plugins = [
    # Plugins
    "pytest_plugins.rerun_rp.rerun_rp",
    # Fixtures
    "pytest_fixtures.api_fixtures",
    # Component Fixtures
    "pytest_fixtures.satellite_auth",
    "pytest_fixtures.templatesync_fixtures",
]
