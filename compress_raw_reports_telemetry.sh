#! /bin/bash

# Deal with spaces in filenames:
# https://www.reeltoreel.nl/wiki/index.php/Dealing_with_spaces_in_filenames
# The default value of IFS is " \t\n" (e.g. <space><tab><newline>)
SAVEIFS=$IFS
IFS=$(echo -en "\n\b")

path="/opt/telemetry/raw/"

for f in `ls -1 $path | grep -v ".gz"`; do
  echo $f
  gzip $path/$f
done

IFS=$SAVEIFS
