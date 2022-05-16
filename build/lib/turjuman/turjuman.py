from asyncio.log import logger
from datetime import datetime
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
from pathlib import Path
import dask.dataframe as dd
import pandas as pd
from hurry.filesize import size
import psutil
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from tqdm import tqdm
import math

class turjuman():
    def __init__(self, logger, cache_dir, model_path=None):
        self.logger = logger
        self.cache_dir=cache_dir
        self.model, self.tokenizer = self.load_model(model_path)
        self.num_cpus=num_cpus=len(psutil.Process().cpu_affinity())

    
    def load_model(self, model_path):
        model_path = model_path if model_path else "UBC-NLP/turjuman"
        self.logger.info("Loading model from {}".format(model_path))
        tokenizer = AutoTokenizer.from_pretrained(model_path, cache_dir=self.cache_dir)  
        model = AutoModelForSeq2SeqLM.from_pretrained(model_path, cache_dir=self.cache_dir)
        return model, tokenizer

    def validate(self, search_method, max_outputs, num_beams):
        validattion_results=None
        if max_outputs> num_beams and search_method=="beam":
            self.logger.error("for `beam search`: `--max_outputs` has to be smaller or equal to `--num_beams`")
        elif max_outputs> 1 and search_method=="greedy":
            self.logger.error("For `greedy search`: `--max_outputs` should be 1")
        else:
            validattion_results="valid"
        
        return validattion_results

    def get_file_content(self, input_file):
        sources=[]
        if Path(input_file).is_file():
            with open(input_file) as f:
                sources = f.read().splitlines()
        else:
             self.logger.error("Can't open the input file {}".format(input_file))
        return sources

    def translate(self, sources, search_method, seq_length, max_outputs, num_beams, no_repeat_ngram_size, top_p, top_k):
        encoding = self.tokenizer(sources,padding=True, return_tensors="pt")
        input_ids, attention_masks = encoding["input_ids"], encoding["attention_mask"]

        if search_method=="greedy":
            self.logger.info("Using greedy search")
            max_outputs=1
            outputs = self.model.generate(
                input_ids=input_ids, attention_mask=attention_masks,
                max_length=seq_length,
                num_return_sequences=max_outputs,
                do_sample=False 
            )
        elif search_method=="beam":
            self.logger.info("Using beam search")
            outputs = self.model.generate(
                input_ids=input_ids, attention_mask=attention_masks,
                max_length=seq_length,
                num_return_sequences=max_outputs,
                num_beams=num_beams, 
                no_repeat_ngram_size=no_repeat_ngram_size, 
                early_stopping=True,
                do_sample=False 
            )
        elif search_method=="sampling":
            self.logger.info("Using sampling search")
            outputs = self.model.generate(
                input_ids=input_ids, attention_mask=attention_masks,
                max_length=seq_length,
                num_return_sequences=max_outputs,
                do_sample=True, 
                top_k=top_k, 
                top_p=top_p 
            )

        generated_text = self.tokenizer.batch_decode(outputs, skip_special_tokens=True)
        targets=[]
        if max_outputs==1:
            targets = generated_text
            outputs={'source':sources, 'target':targets}
        else:
            for i in range(0, len(generated_text), max_outputs):
                translate_start_id= i
                translate_start_end= i+max_outputs
                temp=[]
                for t in range (translate_start_id, translate_start_end):
                    temp.append(generated_text[t])
                targets.append(temp)
            outputs={'source':sources, str(max_outputs)+'_targets':targets}
        return outputs
    
    def translate_batch(self, args):
        batch_id=args['batch_id']
        sources=args['source']
        search_method=args['search_method']
        seq_length=args['seq_length']
        max_outputs=args['max_outputs']
        num_beams=args['num_beams']
        no_repeat_ngram_size=args['no_repeat_ngram_size']
        top_p=args['top_p']
        top_k=args['top_k']
        output=self.translate(sources, search_method, seq_length, max_outputs, num_beams, no_repeat_ngram_size, top_p, top_k)
        self.logger.info("Collecting translation from batch #{}".format(batch_id))
        return {'batch':batch_id, 'output':output}
    def multiprocessing(self, func, args, workers):
        with ProcessPoolExecutor(workers) as ex:
            res = ex.map(func, args)
        return list(res)
    def translate_from_file(self, input_file, search_method, seq_length=512, max_outputs=1, num_beams=5, no_repeat_ngram_size=2, top_p=0.95, top_k=50):
        if self.validate(search_method, max_outputs, num_beams) is None:
            return None
        sources = self.get_file_content(input_file)
        if len(sources)<1:
            self.logger.error("The input file {} is empty".format(input_file))
        output_file = str(Path(input_file).with_suffix(''))+"_Turjuman_translate.json"
        #-- create batches start--#
        
        pd_df = pd.DataFrame.from_dict({'source':sources})
        num_sentences = len(pd_df.index)
        batch_size=25
        num_batches=math.ceil(num_sentences/batch_size)
        ddf = dd.from_pandas(pd_df, npartitions=num_batches)
        num_chuncks=len(ddf.map_partitions(len).compute())
        self.logger.error("The file contains {} sentences/lines, it will process using {} batches".format(num_sentences, num_chuncks))
        batches=[]
        for i in  range (0, num_chuncks):
            print ("loading", i)
            chunck_df = ddf.partitions[i].compute()
            args={
                    'batch_id':(i+1),
                    'source':chunck_df.source.to_list(),
                    'search_method':search_method, 
                    'seq_length':seq_length, 
                    'max_outputs':max_outputs,
                    'num_beams':num_beams,
                    'no_repeat_ngram_size':no_repeat_ngram_size,
                    'top_p':top_p, 
                    'top_k':top_k
                }
            batches.append(args)
            # break
        #-- create batches end--#
        dataframes=[]
        start_generation = datetime.now()
        batches_outputs = self.multiprocessing(self.translate_batch, batches, math.floor(self.num_cpus/2))
        pbar = tqdm(total=len(batches_outputs), desc="Merge batches outpus")
        for output in batches_outputs:
            dataframes.append(pd.DataFrame.from_dict(output))
            pbar.update(1)
        pbar.close()
        end_generation = datetime.now()
        self.logger.info("{} batches translation duration time is {}".format(len(batches), end_generation-start_generation))
        
        # start_generation = datetime.now()
        # batch_tranalation_list = self.multiprocessing(self.translate_batch, batches, self.num_cpus)
        # end_generation = datetime.now()
        # print (" duration", end_generation-start_generation)

        df = pd.concat(dataframes, axis=0, ignore_index=True)
        df.to_json(output_file, orient='records', lines=True)
        self.logger.info("The translation are saved on {}".format(output_file))

    def translate_from_text(self, text, search_method, seq_length=512, max_outputs=1, num_beams=5, no_repeat_ngram_size=2, top_p=0.95, top_k=50):
        if self.validate(search_method, max_outputs, num_beams) is None:
            return None
        sources = [text]
        outputs = self.translate(sources, search_method, seq_length, max_outputs, num_beams, no_repeat_ngram_size, top_p, top_k)
        print ("source: {}".format(outputs['source'][0]))
        targets = outputs['target'][0]
        if type(targets) == list:
            for idx, target in enumerate(targets):
                 print ("target#{}: {}".format(idx+1, target))
        else:
            print ("target: {}".format(targets))
