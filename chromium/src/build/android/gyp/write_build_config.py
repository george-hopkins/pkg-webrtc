#!/usr/bin/env python
#
# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Writes a build_config file.

The build_config file for a target is a json file containing information about
how to build that target based on the target's dependencies. This includes
things like: the javac classpath, the list of android resources dependencies,
etc. It also includes the information needed to create the build_config for
other targets that depend on that one.

Android build scripts should not refer to the build_config directly, and the
build specification should instead pass information in using the special
file-arg syntax (see build_utils.py:ExpandFileArgs). That syntax allows passing
of values in a json dict in a file and looks like this:
  --python-arg=@FileArg(build_config_path:javac:classpath)

Note: If paths to input files are passed in this way, it is important that:
  1. inputs/deps of the action ensure that the files are available the first
  time the action runs.
  2. Either (a) or (b)
    a. inputs/deps ensure that the action runs whenever one of the files changes
    b. the files are added to the action's depfile
"""

import itertools
import optparse
import os
import sys
import xml.dom.minidom

from util import build_utils
from util import md5_check

import write_ordered_libraries


# Types that should never be used as a dependency of another build config.
_ROOT_TYPES = ('android_apk', 'deps_dex', 'java_binary', 'resource_rewriter')
# Types that should not allow code deps to pass through.
_RESOURCE_TYPES = ('android_assets', 'android_resources')


class AndroidManifest(object):
  def __init__(self, path):
    self.path = path
    dom = xml.dom.minidom.parse(path)
    manifests = dom.getElementsByTagName('manifest')
    assert len(manifests) == 1
    self.manifest = manifests[0]

  def GetInstrumentation(self):
    instrumentation_els = self.manifest.getElementsByTagName('instrumentation')
    if len(instrumentation_els) == 0:
      return None
    if len(instrumentation_els) != 1:
      raise Exception(
          'More than one <instrumentation> element found in %s' % self.path)
    return instrumentation_els[0]

  def CheckInstrumentation(self, expected_package):
    instr = self.GetInstrumentation()
    if not instr:
      raise Exception('No <instrumentation> elements found in %s' % self.path)
    instrumented_package = instr.getAttributeNS(
        'http://schemas.android.com/apk/res/android', 'targetPackage')
    if instrumented_package != expected_package:
      raise Exception(
          'Wrong instrumented package. Expected %s, got %s'
          % (expected_package, instrumented_package))

  def GetPackageName(self):
    return self.manifest.getAttribute('package')


dep_config_cache = {}
def GetDepConfig(path):
  if not path in dep_config_cache:
    dep_config_cache[path] = build_utils.ReadJson(path)['deps_info']
  return dep_config_cache[path]


def DepsOfType(wanted_type, configs):
  return [c for c in configs if c['type'] == wanted_type]


def GetAllDepsConfigsInOrder(deps_config_paths):
  def GetDeps(path):
    return set(GetDepConfig(path)['deps_configs'])
  return build_utils.GetSortedTransitiveDependencies(deps_config_paths, GetDeps)


def ResolveGroups(configs):
  while True:
    groups = DepsOfType('group', configs)
    if not groups:
      return configs
    for config in groups:
      index = configs.index(config)
      expanded_configs = [GetDepConfig(p) for p in config['deps_configs']]
      configs[index:index + 1] = expanded_configs


class Deps(object):
  def __init__(self, direct_deps_config_paths):
    self.all_deps_config_paths = GetAllDepsConfigsInOrder(
        direct_deps_config_paths)
    self.direct_deps_configs = ResolveGroups(
        [GetDepConfig(p) for p in direct_deps_config_paths])
    self.all_deps_configs = [
        GetDepConfig(p) for p in self.all_deps_config_paths]
    self.direct_deps_config_paths = direct_deps_config_paths

  def All(self, wanted_type=None):
    if type is None:
      return self.all_deps_configs
    return DepsOfType(wanted_type, self.all_deps_configs)

  def Direct(self, wanted_type=None):
    if wanted_type is None:
      return self.direct_deps_configs
    return DepsOfType(wanted_type, self.direct_deps_configs)

  def AllConfigPaths(self):
    return self.all_deps_config_paths

  def RemoveNonDirectDep(self, path):
    if path in self.direct_deps_config_paths:
      raise Exception('Cannot remove direct dep.')
    self.all_deps_config_paths.remove(path)
    self.all_deps_configs.remove(GetDepConfig(path))

def _MergeAssets(all_assets):
  """Merges all assets from the given deps.

  Returns:
    A tuple of lists: (compressed, uncompressed)
    Each tuple entry is a list of "srcPath:zipPath". srcPath is the path of the
    asset to add, and zipPath is the location within the zip (excluding assets/
    prefix)
  """
  compressed = {}
  uncompressed = {}
  for asset_dep in all_assets:
    entry = asset_dep['assets']
    disable_compression = entry.get('disable_compression', False)
    dest_map = uncompressed if disable_compression else compressed
    other_map = compressed if disable_compression else uncompressed
    outputs = entry.get('outputs', [])
    for src, dest in itertools.izip_longest(entry['sources'], outputs):
      if not dest:
        dest = os.path.basename(src)
      # Merge so that each path shows up in only one of the lists, and that
      # deps of the same target override previous ones.
      other_map.pop(dest, 0)
      dest_map[dest] = src

  def create_list(asset_map):
    ret = ['%s:%s' % (src, dest) for dest, src in asset_map.iteritems()]
    # Sort to ensure deterministic ordering.
    ret.sort()
    return ret

  return create_list(compressed), create_list(uncompressed)


def _FilterUnwantedDepsPaths(dep_paths, target_type):
  # Don't allow root targets to be considered as a dep.
  ret = [p for p in dep_paths if GetDepConfig(p)['type'] not in _ROOT_TYPES]

  # Don't allow java libraries to cross through assets/resources.
  if target_type in _RESOURCE_TYPES:
    ret = [p for p in ret if GetDepConfig(p)['type'] in _RESOURCE_TYPES]
  return ret


def _AsInterfaceJar(jar_path):
  return jar_path[:-3] + 'interface.jar'


def main(argv):
  parser = optparse.OptionParser()
  build_utils.AddDepfileOption(parser)
  parser.add_option('--build-config', help='Path to build_config output.')
  parser.add_option(
      '--type',
      help='Type of this target (e.g. android_library).')
  parser.add_option(
      '--possible-deps-configs',
      help='List of paths for dependency\'s build_config files. Some '
      'dependencies may not write build_config files. Missing build_config '
      'files are handled differently based on the type of this target.')

  # android_resources options
  parser.add_option('--srcjar', help='Path to target\'s resources srcjar.')
  parser.add_option('--resources-zip', help='Path to target\'s resources zip.')
  parser.add_option('--r-text', help='Path to target\'s R.txt file.')
  parser.add_option('--package-name',
      help='Java package name for these resources.')
  parser.add_option('--android-manifest', help='Path to android manifest.')
  parser.add_option('--is-locale-resource', action='store_true',
                    help='Whether it is locale resource.')
  parser.add_option('--resource-dirs', action='append', default=[],
                    help='GYP-list of resource dirs')

  # android_assets options
  parser.add_option('--asset-sources', help='List of asset sources.')
  parser.add_option('--asset-renaming-sources',
                    help='List of asset sources with custom destinations.')
  parser.add_option('--asset-renaming-destinations',
                    help='List of asset custom destinations.')
  parser.add_option('--disable-asset-compression', action='store_true',
                    help='Whether to disable asset compression.')

  # java library options
  parser.add_option('--jar-path', help='Path to target\'s jar output.')
  parser.add_option('--supports-android', action='store_true',
      help='Whether this library supports running on the Android platform.')
  parser.add_option('--requires-android', action='store_true',
      help='Whether this library requires running on the Android platform.')
  parser.add_option('--bypass-platform-checks', action='store_true',
      help='Bypass checks for support/require Android platform.')

  # android library options
  parser.add_option('--dex-path', help='Path to target\'s dex output.')

  # native library options
  parser.add_option('--native-libs', help='List of top-level native libs.')
  parser.add_option('--readelf-path', help='Path to toolchain\'s readelf.')

  # apk options
  parser.add_option('--apk-path', help='Path to the target\'s apk output.')
  parser.add_option('--incremental-apk-path',
                    help="Path to the target's incremental apk output.")
  parser.add_option('--incremental-install-script-path',
                    help="Path to the target's generated incremental install "
                    "script.")

  parser.add_option('--tested-apk-config',
      help='Path to the build config of the tested apk (for an instrumentation '
      'test apk).')
  parser.add_option('--proguard-enabled', action='store_true',
      help='Whether proguard is enabled for this apk.')
  parser.add_option('--proguard-info',
      help='Path to the proguard .info output for this apk.')
  parser.add_option('--has-alternative-locale-resource', action='store_true',
      help='Whether there is alternative-locale-resource in direct deps')

  options, args = parser.parse_args(argv)

  if args:
    parser.error('No positional arguments should be given.')

  required_options_map = {
      'java_binary': ['build_config', 'jar_path'],
      'java_library': ['build_config', 'jar_path'],
      'android_assets': ['build_config'],
      'android_resources': ['build_config', 'resources_zip'],
      'android_apk': ['build_config', 'jar_path', 'dex_path', 'resources_zip'],
      'deps_dex': ['build_config', 'dex_path'],
      'resource_rewriter': ['build_config'],
      'group': ['build_config'],
  }
  required_options = required_options_map.get(options.type)
  if not required_options:
    raise Exception('Unknown type: <%s>' % options.type)

  if options.native_libs:
    required_options.append('readelf_path')

  build_utils.CheckOptions(options, parser, required_options)

  if options.type == 'java_library':
    if options.supports_android and not options.dex_path:
      raise Exception('java_library that supports Android requires a dex path.')

    if options.requires_android and not options.supports_android:
      raise Exception(
          '--supports-android is required when using --requires-android')

  possible_deps_config_paths = build_utils.ParseGypList(
      options.possible_deps_configs)

  unknown_deps = [
      c for c in possible_deps_config_paths if not os.path.exists(c)]

  direct_deps_config_paths = [
      c for c in possible_deps_config_paths if not c in unknown_deps]
  direct_deps_config_paths = _FilterUnwantedDepsPaths(direct_deps_config_paths,
                                                      options.type)

  deps = Deps(direct_deps_config_paths)
  all_inputs = deps.AllConfigPaths() + build_utils.GetPythonDependencies()

  # Remove other locale resources if there is alternative_locale_resource in
  # direct deps.
  if options.has_alternative_locale_resource:
    alternative = [r['path'] for r in deps.Direct('android_resources')
                   if r.get('is_locale_resource')]
    # We can only have one locale resources in direct deps.
    if len(alternative) != 1:
      raise Exception('The number of locale resource in direct deps is wrong %d'
                       % len(alternative))
    unwanted = [r['path'] for r in deps.All('android_resources')
                if r.get('is_locale_resource') and r['path'] not in alternative]
    for p in unwanted:
      deps.RemoveNonDirectDep(p)


  direct_library_deps = deps.Direct('java_library')
  all_library_deps = deps.All('java_library')

  direct_resources_deps = deps.Direct('android_resources')
  all_resources_deps = deps.All('android_resources')
  # Resources should be ordered with the highest-level dependency first so that
  # overrides are done correctly.
  all_resources_deps.reverse()

  if options.type == 'android_apk' and options.tested_apk_config:
    tested_apk_deps = Deps([options.tested_apk_config])
    tested_apk_resources_deps = tested_apk_deps.All('android_resources')
    all_resources_deps = [
        d for d in all_resources_deps if not d in tested_apk_resources_deps]

  # Initialize some common config.
  config = {
    'deps_info': {
      'name': os.path.basename(options.build_config),
      'path': options.build_config,
      'type': options.type,
      'deps_configs': direct_deps_config_paths
    }
  }
  deps_info = config['deps_info']

  if (options.type in ('java_binary', 'java_library') and
      not options.bypass_platform_checks):
    deps_info['requires_android'] = options.requires_android
    deps_info['supports_android'] = options.supports_android

    deps_require_android = (all_resources_deps +
        [d['name'] for d in all_library_deps if d['requires_android']])
    deps_not_support_android = (
        [d['name'] for d in all_library_deps if not d['supports_android']])

    if deps_require_android and not options.requires_android:
      raise Exception('Some deps require building for the Android platform: ' +
          str(deps_require_android))

    if deps_not_support_android and options.supports_android:
      raise Exception('Not all deps support the Android platform: ' +
          str(deps_not_support_android))

  if options.type in ('java_binary', 'java_library', 'android_apk'):
    deps_info['resources_deps'] = [c['path'] for c in all_resources_deps]
    deps_info['jar_path'] = options.jar_path
    if options.type == 'android_apk' or options.supports_android:
      deps_info['dex_path'] = options.dex_path
    if options.type == 'android_apk':
      deps_info['apk_path'] = options.apk_path
      deps_info['incremental_apk_path'] = options.incremental_apk_path
      deps_info['incremental_install_script_path'] = (
          options.incremental_install_script_path)

    # Classpath values filled in below (after applying tested_apk_config).
    config['javac'] = {}

  if options.type in ('java_binary', 'java_library'):
    # Only resources might have srcjars (normal srcjar targets are listed in
    # srcjar_deps). A resource's srcjar contains the R.java file for those
    # resources, and (like Android's default build system) we allow a library to
    # refer to the resources in any of its dependents.
    config['javac']['srcjars'] = [
        c['srcjar'] for c in all_resources_deps if 'srcjar' in c]

    # Used to strip out R.class for android_prebuilt()s.
    if options.type == 'java_library':
      config['javac']['resource_packages'] = [
          c['package_name'] for c in all_resources_deps if 'package_name' in c]

  if options.type == 'android_apk':
    # Apks will get their resources srcjar explicitly passed to the java step.
    config['javac']['srcjars'] = []

  if options.type == 'android_assets':
    all_asset_sources = []
    if options.asset_renaming_sources:
      all_asset_sources.extend(
          build_utils.ParseGypList(options.asset_renaming_sources))
    if options.asset_sources:
      all_asset_sources.extend(build_utils.ParseGypList(options.asset_sources))

    deps_info['assets'] = {
        'sources': all_asset_sources
    }
    if options.asset_renaming_destinations:
      deps_info['assets']['outputs'] = (
          build_utils.ParseGypList(options.asset_renaming_destinations))
    if options.disable_asset_compression:
      deps_info['assets']['disable_compression'] = True

  if options.type == 'android_resources':
    deps_info['resources_zip'] = options.resources_zip
    if options.srcjar:
      deps_info['srcjar'] = options.srcjar
    if options.android_manifest:
      manifest = AndroidManifest(options.android_manifest)
      deps_info['package_name'] = manifest.GetPackageName()
    if options.package_name:
      deps_info['package_name'] = options.package_name
    if options.r_text:
      deps_info['r_text'] = options.r_text
    if options.is_locale_resource:
      deps_info['is_locale_resource'] = True
    # Record resources_dirs of this target so dependendent libraries can pick up
    # them and pass to Lint.
    lint_info = deps_info['lint'] = {}
    resource_dirs = []
    lint_info['resources_zips'] = []
    for gyp_list in options.resource_dirs:
      resource_dirs += build_utils.ParseGypList(gyp_list)
    if resource_dirs:
      lint_info['resources_dirs'] = resource_dirs
    # There things become ugly. Resource targets may have resource dependencies
    # as well. Some of these dependencies are resources from other libraries
    # so we should not lint them here (they should be linted within their
    # libraries). But others are just generated resources that also contribute
    # to this library and we should check them. These generated resources has no
    # package_name so we skip all direct deps that has package names.
    for c in direct_resources_deps:
      if 'package_name' not in c:
        lint_info['resources_zips'].append(c['resources_zip'])

  if options.supports_android and options.type in ('android_apk',
                                                   'java_library'):
    # GN's project model doesn't exactly match traditional Android project
    # model: GN splits resources into separate targets, while in Android
    # resources are part of the library/APK. Android Lint expects an Android
    # project - with java sources and resources combined. So we assume that
    # direct resource dependencies of the library/APK are the resources of this
    # library in Android project sense.
    lint_info = config['lint'] = {}
    lint_info['resources_dirs'] = []
    lint_info['resources_zips'] = []
    for c in direct_resources_deps:
      lint_info['resources_dirs'] += c['lint'].get('resources_dirs', [])
      lint_info['resources_zips'] += c['lint'].get('resources_zips', [])


  if options.type in ('android_resources','android_apk', 'resource_rewriter'):
    config['resources'] = {}
    config['resources']['dependency_zips'] = [
        c['resources_zip'] for c in all_resources_deps]
    config['resources']['extra_package_names'] = []
    config['resources']['extra_r_text_files'] = []

  if options.type == 'android_apk' or options.type == 'resource_rewriter':
    config['resources']['extra_package_names'] = [
        c['package_name'] for c in all_resources_deps if 'package_name' in c]
    config['resources']['extra_r_text_files'] = [
        c['r_text'] for c in all_resources_deps if 'r_text' in c]

  if options.type in ['android_apk', 'deps_dex']:
    deps_dex_files = [c['dex_path'] for c in all_library_deps]

  if options.type in ('java_binary', 'java_library', 'android_apk'):
    javac_classpath = [c['jar_path'] for c in direct_library_deps]
    java_full_classpath = [c['jar_path'] for c in all_library_deps]

  # An instrumentation test apk should exclude the dex files that are in the apk
  # under test.
  if options.type == 'android_apk' and options.tested_apk_config:
    tested_apk_config = GetDepConfig(options.tested_apk_config)

    expected_tested_package = tested_apk_config['package_name']
    AndroidManifest(options.android_manifest).CheckInstrumentation(
        expected_tested_package)
    if tested_apk_config['proguard_enabled']:
      assert options.proguard_enabled, ('proguard must be enabled for '
          'instrumentation apks if it\'s enabled for the tested apk.')

    # Include in the classpath classes that are added directly to the apk under
    # test (those that are not a part of a java_library).
    javac_classpath.append(tested_apk_config['jar_path'])
    java_full_classpath.append(tested_apk_config['jar_path'])

    # Exclude dex files from the test apk that exist within the apk under test.
    # TODO(agrieve): When proguard is enabled, this filtering logic happens
    #     within proguard_util.py. Move the logic for the proguard case into
    #     here as well.
    tested_apk_library_deps = tested_apk_deps.All('java_library')
    tested_apk_deps_dex_files = [c['dex_path'] for c in tested_apk_library_deps]
    deps_dex_files = [
        p for p in deps_dex_files if not p in tested_apk_deps_dex_files]

  if options.type == 'android_apk':
    deps_info['proguard_enabled'] = options.proguard_enabled
    deps_info['proguard_info'] = options.proguard_info
    config['proguard'] = {}
    proguard_config = config['proguard']
    proguard_config['input_paths'] = [options.jar_path] + java_full_classpath

  # Dependencies for the final dex file of an apk or a 'deps_dex'.
  if options.type in ['android_apk', 'deps_dex']:
    config['final_dex'] = {}
    dex_config = config['final_dex']
    dex_config['dependency_dex_files'] = deps_dex_files

  if options.type in ('java_binary', 'java_library', 'android_apk'):
    config['javac']['classpath'] = javac_classpath
    config['javac']['interface_classpath'] = [
        _AsInterfaceJar(p) for p in javac_classpath]
    config['java'] = {
      'full_classpath': java_full_classpath
    }

  if options.type == 'android_apk':
    dependency_jars = [c['jar_path'] for c in all_library_deps]
    all_interface_jars = [
        _AsInterfaceJar(p) for p in dependency_jars + [options.jar_path]]
    config['dist_jar'] = {
      'dependency_jars': dependency_jars,
      'all_interface_jars': all_interface_jars,
    }
    manifest = AndroidManifest(options.android_manifest)
    deps_info['package_name'] = manifest.GetPackageName()
    if not options.tested_apk_config and manifest.GetInstrumentation():
      # This must then have instrumentation only for itself.
      manifest.CheckInstrumentation(manifest.GetPackageName())

    library_paths = []
    java_libraries_list_holder = [None]
    libraries = build_utils.ParseGypList(options.native_libs or '[]')
    if libraries:
      def recompute_ordered_libraries():
        libraries_dir = os.path.dirname(libraries[0])
        write_ordered_libraries.SetReadelfPath(options.readelf_path)
        write_ordered_libraries.SetLibraryDirs([libraries_dir])
        all_deps = (
            write_ordered_libraries.GetSortedTransitiveDependenciesForBinaries(
                libraries))
        # Create a java literal array with the "base" library names:
        # e.g. libfoo.so -> foo
        java_libraries_list_holder[0] = ('{%s}' % ','.join(
            ['"%s"' % s[3:-3] for s in all_deps]))
        library_paths.extend(
            write_ordered_libraries.FullLibraryPath(x) for x in all_deps)

      # This step takes about 600ms on a z620 for chrome_apk, so it's worth
      # caching.
      md5_check.CallAndRecordIfStale(
          recompute_ordered_libraries,
          record_path=options.build_config + '.nativelibs.md5.stamp',
          input_paths=libraries,
          output_paths=[options.build_config])
      if not library_paths:
        prev_config = build_utils.ReadJson(options.build_config)
        java_libraries_list_holder[0] = (
            prev_config['native']['java_libraries_list'])
        library_paths.extend(prev_config['native']['libraries'])

    all_inputs.extend(library_paths)
    config['native'] = {
      'libraries': library_paths,
      'java_libraries_list': java_libraries_list_holder[0],
    }
    config['assets'], config['uncompressed_assets'] = (
        _MergeAssets(deps.All('android_assets')))

  build_utils.WriteJson(config, options.build_config, only_if_changed=True)

  if options.depfile:
    build_utils.WriteDepfile(options.depfile, all_inputs)


if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))
