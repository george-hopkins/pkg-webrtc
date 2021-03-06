// Copyright 2016 The Chromium Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "tools/gn/ninja_create_bundle_target_writer.h"

#include <algorithm>
#include <sstream>

#include "testing/gtest/include/gtest/gtest.h"
#include "tools/gn/target.h"
#include "tools/gn/test_with_scope.h"

namespace {

void SetupBundleDataDir(BundleData* bundle_data, const std::string& root_dir) {
  std::string bundle_root_dir = root_dir + "/bar.bundle";
  bundle_data->root_dir() = SourceDir(bundle_root_dir);
  bundle_data->resources_dir() = SourceDir(bundle_root_dir + "/Resources");
  bundle_data->executable_dir() = SourceDir(bundle_root_dir + "/Executable");
  bundle_data->plugins_dir() = SourceDir(bundle_root_dir + "/PlugIns");
}

}  // namespace

// Tests multiple files with an output pattern.
TEST(NinjaCreateBundleTargetWriter, Run) {
  TestWithScope setup;
  Err err;

  setup.build_settings()->SetBuildDir(SourceDir("//out/Debug/"));
  Target target(setup.settings(), Label(SourceDir("//baz/"), "bar"));
  target.set_output_type(Target::CREATE_BUNDLE);

  SetupBundleDataDir(&target.bundle_data(), "//out/Debug");

  std::vector<SourceFile> sources;
  sources.push_back(SourceFile("//foo/input1.txt"));
  sources.push_back(SourceFile("//foo/input2.txt"));
  target.bundle_data().file_rules().push_back(BundleFileRule(
      sources, SubstitutionPattern::MakeForTest(
                   "{{bundle_resources_dir}}/{{source_file_part}}")));

  target.SetToolchain(setup.toolchain());
  ASSERT_TRUE(target.OnResolved(&err));

  std::ostringstream out;
  NinjaCreateBundleTargetWriter writer(&target, out);
  writer.Run();

  const char expected[] =
      "build bar.bundle/Resources/input1.txt: copy_bundle_data "
          "../../foo/input1.txt\n"
      "build bar.bundle/Resources/input2.txt: copy_bundle_data "
          "../../foo/input2.txt\n"
      "build obj/baz/bar.stamp: stamp "
          "bar.bundle/Resources/input1.txt "
          "bar.bundle/Resources/input2.txt\n"
      "build bar.bundle: phony obj/baz/bar.stamp\n";
  std::string out_str = out.str();
  EXPECT_EQ(expected, out_str);
}

// Tests multiple files from asset catalog.
TEST(NinjaCreateBundleTargetWriter, AssetCatalog) {
  TestWithScope setup;
  Err err;

  setup.build_settings()->SetBuildDir(SourceDir("//out/Debug/"));
  Target target(setup.settings(), Label(SourceDir("//baz/"), "bar"));
  target.set_output_type(Target::CREATE_BUNDLE);

  SetupBundleDataDir(&target.bundle_data(), "//out/Debug");

  std::vector<SourceFile>& asset_catalog_sources =
      target.bundle_data().asset_catalog_sources();
  asset_catalog_sources.push_back(
      SourceFile("//foo/Foo.xcassets/foo.imageset/Contents.json"));
  asset_catalog_sources.push_back(
      SourceFile("//foo/Foo.xcassets/foo.imageset/FooIcon-29.png"));
  asset_catalog_sources.push_back(
      SourceFile("//foo/Foo.xcassets/foo.imageset/FooIcon-29@2x.png"));
  asset_catalog_sources.push_back(
      SourceFile("//foo/Foo.xcassets/foo.imageset/FooIcon-29@3x.png"));

  target.SetToolchain(setup.toolchain());
  ASSERT_TRUE(target.OnResolved(&err));

  std::ostringstream out;
  NinjaCreateBundleTargetWriter writer(&target, out);
  writer.Run();

  const char expected[] =
      "build bar.bundle/Resources/Assets.car: compile_xcassets "
          "../../foo/Foo.xcassets | "
          "../../foo/Foo.xcassets/foo.imageset/Contents.json "
          "../../foo/Foo.xcassets/foo.imageset/FooIcon-29.png "
          "../../foo/Foo.xcassets/foo.imageset/FooIcon-29@2x.png "
          "../../foo/Foo.xcassets/foo.imageset/FooIcon-29@3x.png\n"
      "build obj/baz/bar.stamp: stamp bar.bundle/Resources/Assets.car\n"
      "build bar.bundle: phony obj/baz/bar.stamp\n";
  std::string out_str = out.str();
  EXPECT_EQ(expected, out_str);
}

// Tests that the phony target for the top-level bundle directory is generated
// correctly.
TEST(NinjaCreateBundleTargetWriter, BundleRootDirOutput) {
  TestWithScope setup;
  Err err;

  setup.build_settings()->SetBuildDir(SourceDir("//out/Debug/"));
  Target target(setup.settings(), Label(SourceDir("//baz/"), "bar"));
  target.set_output_type(Target::CREATE_BUNDLE);

  const std::string bundle_root_dir("//out/Debug/bar.bundle/Contents");
  target.bundle_data().root_dir() = SourceDir(bundle_root_dir);
  target.bundle_data().resources_dir() =
      SourceDir(bundle_root_dir + "/Resources");
  target.bundle_data().executable_dir() = SourceDir(bundle_root_dir + "/MacOS");
  target.bundle_data().plugins_dir() = SourceDir(bundle_root_dir + "/Plug Ins");

  std::vector<SourceFile> sources;
  sources.push_back(SourceFile("//foo/input1.txt"));
  sources.push_back(SourceFile("//foo/input2.txt"));
  target.bundle_data().file_rules().push_back(BundleFileRule(
      sources, SubstitutionPattern::MakeForTest(
                   "{{bundle_resources_dir}}/{{source_file_part}}")));

  target.SetToolchain(setup.toolchain());
  ASSERT_TRUE(target.OnResolved(&err));

  std::ostringstream out;
  NinjaCreateBundleTargetWriter writer(&target, out);
  writer.Run();

  const char expected[] =
      "build bar.bundle/Contents/Resources/input1.txt: copy_bundle_data "
          "../../foo/input1.txt\n"
      "build bar.bundle/Contents/Resources/input2.txt: copy_bundle_data "
          "../../foo/input2.txt\n"
      "build obj/baz/bar.stamp: stamp "
          "bar.bundle/Contents/Resources/input1.txt "
          "bar.bundle/Contents/Resources/input2.txt\n"
      "build bar.bundle: phony obj/baz/bar.stamp\n";
  std::string out_str = out.str();
  EXPECT_EQ(expected, out_str);
}

// Tests complex target with multiple bundle_data sources, including
// some asset catalog.
TEST(NinjaCreateBundleTargetWriter, ImplicitDeps) {
  TestWithScope setup;
  Err err;

  setup.build_settings()->SetBuildDir(SourceDir("//out/Debug/"));
  Target target(setup.settings(), Label(SourceDir("//baz/"), "bar"));
  target.set_output_type(Target::CREATE_BUNDLE);

  SetupBundleDataDir(&target.bundle_data(), "//out/Debug");

  std::vector<SourceFile> sources1;
  sources1.push_back(SourceFile("//foo/input1.txt"));
  sources1.push_back(SourceFile("//foo/input2.txt"));
  target.bundle_data().file_rules().push_back(BundleFileRule(
      sources1, SubstitutionPattern::MakeForTest(
                    "{{bundle_resources_dir}}/{{source_file_part}}")));

  std::vector<SourceFile> sources2;
  sources2.push_back(SourceFile("//qux/Info.plist"));
  target.bundle_data().file_rules().push_back(BundleFileRule(
      sources2,
      SubstitutionPattern::MakeForTest("{{bundle_root_dir}}/Info.plist")));

  std::vector<SourceFile> empty_source;
  target.bundle_data().file_rules().push_back(BundleFileRule(
      empty_source, SubstitutionPattern::MakeForTest(
                        "{{bundle_plugins_dir}}/{{source_file_part}}")));

  std::vector<SourceFile>& asset_catalog_sources =
      target.bundle_data().asset_catalog_sources();
  asset_catalog_sources.push_back(
      SourceFile("//foo/Foo.xcassets/foo.imageset/Contents.json"));
  asset_catalog_sources.push_back(
      SourceFile("//foo/Foo.xcassets/foo.imageset/FooIcon-29.png"));
  asset_catalog_sources.push_back(
      SourceFile("//foo/Foo.xcassets/foo.imageset/FooIcon-29@2x.png"));
  asset_catalog_sources.push_back(
      SourceFile("//foo/Foo.xcassets/foo.imageset/FooIcon-29@3x.png"));

  target.SetToolchain(setup.toolchain());
  ASSERT_TRUE(target.OnResolved(&err));

  std::ostringstream out;
  NinjaCreateBundleTargetWriter writer(&target, out);
  writer.Run();

  const char expected[] =
      "build bar.bundle/Resources/input1.txt: copy_bundle_data "
          "../../foo/input1.txt\n"
      "build bar.bundle/Resources/input2.txt: copy_bundle_data "
          "../../foo/input2.txt\n"
      "build bar.bundle/Info.plist: copy_bundle_data "
          "../../qux/Info.plist\n"
      "build bar.bundle/Resources/Assets.car: compile_xcassets "
          "../../foo/Foo.xcassets | "
          "../../foo/Foo.xcassets/foo.imageset/Contents.json "
          "../../foo/Foo.xcassets/foo.imageset/FooIcon-29.png "
          "../../foo/Foo.xcassets/foo.imageset/FooIcon-29@2x.png "
          "../../foo/Foo.xcassets/foo.imageset/FooIcon-29@3x.png\n"
      "build obj/baz/bar.stamp: stamp "
          "bar.bundle/Resources/input1.txt "
          "bar.bundle/Resources/input2.txt "
          "bar.bundle/Info.plist "
          "bar.bundle/Resources/Assets.car\n"
      "build bar.bundle: phony obj/baz/bar.stamp\n";
  std::string out_str = out.str();
  EXPECT_EQ(expected, out_str);
}

// Tests multiple files with an output pattern.
TEST(NinjaCreateBundleTargetWriter, CodeSigning) {
  TestWithScope setup;
  Err err;

  setup.build_settings()->SetBuildDir(SourceDir("//out/Debug/"));

  // Simulate a binary build by another target. Since no toolchain is defined
  // use an action instead of an executable target for simplicity.
  Target binary(setup.settings(), Label(SourceDir("//baz/"), "quz"));
  binary.set_output_type(Target::EXECUTABLE);
  binary.visibility().SetPublic();
  binary.sources().push_back(SourceFile("//baz/quz.c"));
  binary.set_output_name("obj/baz/quz/bin");
  binary.set_output_prefix_override(true);
  binary.SetToolchain(setup.toolchain());
  ASSERT_TRUE(binary.OnResolved(&err));

  Target target(setup.settings(),
      Label(SourceDir("//baz/"), "bar",
      setup.toolchain()->label().dir(),
      setup.toolchain()->label().name()));
  target.set_output_type(Target::CREATE_BUNDLE);

  SetupBundleDataDir(&target.bundle_data(), "//out/Debug");

  std::vector<SourceFile> sources;
  sources.push_back(SourceFile("//foo/input1.txt"));
  sources.push_back(SourceFile("//foo/input2.txt"));
  target.bundle_data().file_rules().push_back(BundleFileRule(
      sources, SubstitutionPattern::MakeForTest(
                   "{{bundle_resources_dir}}/{{source_file_part}}")));

  target.bundle_data().set_code_signing_script(
      SourceFile("//build/codesign.py"));
  target.bundle_data().code_signing_sources().push_back(
      SourceFile("//out/Debug/obj/baz/quz/bin"));
  target.bundle_data().code_signing_outputs() = SubstitutionList::MakeForTest(
      "//out/Debug/bar.bundle/quz",
      "//out/Debug/bar.bundle/_CodeSignature/CodeResources");
  target.bundle_data().code_signing_args() = SubstitutionList::MakeForTest(
      "-b=obj/baz/quz/bin",
      "bar.bundle");

  target.public_deps().push_back(LabelTargetPair(&binary));

  target.SetToolchain(setup.toolchain());
  ASSERT_TRUE(target.OnResolved(&err));

  std::ostringstream out;
  NinjaCreateBundleTargetWriter writer(&target, out);
  writer.Run();

  const char expected[] =
      "rule __baz_bar___toolchain_default__code_signing_rule\n"
      "  command =  ../../build/codesign.py -b=obj/baz/quz/bin bar.bundle\n"
      "  description = CODE SIGNING //baz:bar(//toolchain:default)\n"
      "  restat = 1\n"
      "\n"
      "build bar.bundle/Resources/input1.txt: copy_bundle_data "
          "../../foo/input1.txt\n"
      "build bar.bundle/Resources/input2.txt: copy_bundle_data "
          "../../foo/input2.txt\n"
      "build obj/baz/bar.codesigning.inputdeps.stamp: stamp "
          "../../build/codesign.py "
          "obj/baz/quz/bin "
          "bar.bundle/Resources/input1.txt "
          "bar.bundle/Resources/input2.txt\n"
      "build bar.bundle/quz bar.bundle/_CodeSignature/CodeResources: "
          "__baz_bar___toolchain_default__code_signing_rule "
          "| obj/baz/bar.codesigning.inputdeps.stamp\n"
      "build obj/baz/bar.stamp: stamp "
          "bar.bundle/quz "
          "bar.bundle/_CodeSignature/CodeResources\n"
      "build bar.bundle: phony obj/baz/bar.stamp\n";
  std::string out_str = out.str();
  EXPECT_EQ(expected, out_str);
}
