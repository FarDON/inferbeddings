python3 ./bin/nli-cli.py -f -n -m ff-dam --batch-size 32 --dropout-keep-prob 0.8 --representation-size 200 --optimizer adagrad --learning-rate 0.05 -c 100 -i uniform --nb-epochs 1000 --has-bos --has-unk -p --glove $HOME/data/glove/glove.840B.300d.txt -S --save 03092017/dam_1
